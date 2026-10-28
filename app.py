#!/usr/bin/env python3
"""
Peridot "60-Second System" — hackathon demo service.

SIMULATED DEMO — no real messages are sent, no real calls are made.
All telephony is SIMULATED: a "missed call" is a JSON POST, a "textback"
is a line in an outbox log + a row in a web thread view. Nothing here
touches a carrier, Twilio, or any paid service.

Stdlib only (http.server). No pip installs, no API key required.

Run:
    python3 app.py                 # http://127.0.0.1:8765/
    PORT=9000 python3 app.py

Endpoints:
    GET  /                  dashboard (HTML) — lists leads
    GET  /lead/<lead_id>    one lead: full timeline + SIMULATED thread view
    GET  /api/leads         all leads (JSON)
    GET  /api/leads/<id>    one lead (JSON)
    GET  /api/slots         slot store (JSON)
    POST /missed-call       SIMULATED carrier webhook:
                            {"caller_number": "+1...", "caller_name": "...",
                             "timestamp": "<ISO-8601>"}
    POST /reply             SIMULATED inbound SMS:
                            {"lead_id": "...", "message": "..."}
                            (or {"caller_number": "+1...", "message": "..."})

LLM backends (LLM_BACKEND env var):
    mock  (default) deterministic scripted qualification — mock_responder.py
    meta            Meta Model API — meta_client.py, key in META_MODEL_API_KEY.
                    If the Meta client is not configured, chat generation
                    falls back to the mock responder (the qualification
                    state machine stays deterministic either way, so the
                    demo and its e2e test remain reproducible).
"""

import html
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from mock_responder import MockResponder, detect_urgency

BASE_DIR = Path(__file__).resolve().parent
SLOTS_FILE = Path(os.environ.get("DEMO_SLOTS_FILE", BASE_DIR / "slots.json"))
LEADS_FILE = Path(os.environ.get("DEMO_LEADS_FILE", BASE_DIR / "leads.json"))
OUTBOX_FILE = Path(os.environ.get("DEMO_OUTBOX_FILE", BASE_DIR / "outbox.log"))
LLM_BACKEND = os.environ.get("LLM_BACKEND", "mock").lower()

_lock = threading.Lock()
_responder = MockResponder()


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_ts(value):
    """Parse an ISO-8601 timestamp; return aware datetime or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return default
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_leads():
    return load_json(LEADS_FILE, [])


def save_leads(leads):
    save_json(LEADS_FILE, leads)


def append_outbox(entry):
    """Append one SIMULATED SMS to the outbox log."""
    entry = {"simulated": True, "channel": "SIMULATED_SMS", **entry}
    with OUTBOX_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def add_event(lead, kind, text, **extra):
    event = {"kind": kind, "timestamp": now_iso(), "text": text, **extra}
    lead["timeline"].append(event)
    lead["thread"].append(event)
    return event


def send_simulated_sms(lead, text):
    """'Send' a SIMULATED SMS: outbox log + thread view only. No carrier."""
    entry = append_outbox(
        {"to": lead["caller_number"], "lead_id": lead["id"],
         "timestamp": now_iso(), "body": text}
    )
    return add_event(lead, "textback_or_sms_sent_SIMULATED", text,
                     timestamp=entry["timestamp"])


# --------------------------------------------------------------------------
# Booking
# --------------------------------------------------------------------------

def book_next_slot(lead):
    """Pick the next open slot from the local slot store and mark it taken.

    ======================================================================
    GOOGLE CALENDAR HOOK — REAL INTEGRATION GOES HERE
    In production, replace this local-JSON lookup with a Google Calendar
    call: read free/busy from the contractor's calendar, create a real
    calendar event for the winning slot, and store the returned
    Google Calendar event ID on the booking (lead["booking"]["calendar_event_id"]).
    Everything downstream (timeline event, thread message, dashboard)
    already consumes lead["booking"], so only this function changes.
    ======================================================================
    """
    store = load_json(SLOTS_FILE, {"slots": []})
    for slot in store.get("slots", []):
        if not slot.get("taken"):
            slot["taken"] = True
            slot["booked_by"] = lead["id"]
            save_json(SLOTS_FILE, store)
            booking = {
                "slot_id": slot["id"],
                "slot_label": slot["label"],
                "slot_start": slot["start"],
                "priority": lead.get("path") == "urgent",
                "calendar_event_id": None,  # <- Google Calendar event ID lands here
                "booked_at": now_iso(),
                "simulated": True,
            }
            lead["booking"] = booking
            add_event(
                lead, "appointment_booked_SIMULATED",
                f"Booked: {slot['label']}"
                + (" (PRIORITY — urgent job)" if booking["priority"] else "")
                + " [SIMULATED booking, local slot store]",
                booking=booking, timestamp=booking["booked_at"],
            )
            confirm = (
                f"You're booked: {slot['label']}"
                + (" — PRIORITY slot for your urgent job." if booking["priority"]
                   else " for your estimate visit.")
                + " We'll send a reminder before then. (SIMULATED message — no real SMS was sent.)"
            )
            send_simulated_sms(lead, confirm)
            return booking
    add_event(lead, "booking_failed", "No open slots left in the local slot store.")
    return None


# --------------------------------------------------------------------------
# LLM adapter layer
# --------------------------------------------------------------------------

def generate_reply(stage_completed, lead):
    """Return (text, next_stage) from the configured LLM backend.

    The scripted state transition (next_stage, and the facts/labels in the
    text) always comes from the deterministic responder, so the flow stays
    reproducible. Meta backend (LLM_BACKEND=meta + credential configured):
    the real Meta model rephrases the scripted text — same facts, same
    question, labels preserved — which is the real-model path verified
    end-to-end on Oct 23, 2026 (see
    ~/workspace/hackathon/real-model-status-2026-10-23.md).
    Any Meta error or an unconfigured client falls back to the scripted
    text (documented behaviour), so the demo never dead-ends.
    """
    text, next_stage = _responder.reply_for_completed_stage(stage_completed, lead)
    if LLM_BACKEND == "meta":
        # Imported lazily so the demo never requires the module's config.
        from meta_client import MetaModelClient, MetaClientError
        client = MetaModelClient()
        if client.configured:
            try:
                rephrased = client.chat([
                    {"role": "system", "content": (
                        "You are the text-message voice of CLA Peridot, a "
                        "contracting company, inside a SIMULATED demo. "
                        "Rewrite the given outbound message in a natural, "
                        "warm texting tone. Keep every fact and the exact "
                        "question being asked. Keep the markers 'URGENT "
                        "PATH:' / 'PLANNED PATH:' and any '(SIMULATED)' "
                        "label exactly as written if present. Change "
                        "nothing else about the meaning. Reply with the "
                        "rewritten message only."
                    )},
                    {"role": "user", "content": text},
                ])
                if rephrased and rephrased.strip():
                    return rephrased.strip(), next_stage
            except MetaClientError:
                pass  # fall through to the scripted text
    return text, next_stage


# --------------------------------------------------------------------------
# Core flows
# --------------------------------------------------------------------------

def create_lead(payload):
    caller_number = str(payload.get("caller_number") or payload.get("from") or "").strip()
    if not caller_number:
        raise ValueError("caller_number is required")
    caller_name = payload.get("caller_name") or payload.get("name")
    missed_dt = parse_ts(payload.get("timestamp")) or datetime.now(timezone.utc)
    missed_iso = missed_dt.isoformat()

    lead = {
        "id": "lead-" + uuid.uuid4().hex[:8],
        "simulated": True,
        "caller_number": caller_number,
        "caller_name": caller_name,
        "status": "qualifying",
        "stage": "name",  # the textback asks for the name first
        "path": None,      # "urgent" | "planned" once timeline is answered
        "qualification": {},
        "booking": None,
        "created_at": now_iso(),
        "timeline": [],
        "thread": [],
    }
    add_event(lead, "missed_call_SIMULATED",
              f"SIMULATED missed call from {caller_number}"
              + (f" ({caller_name})" if caller_name else ""),
              timestamp=missed_iso)

    # --- Textback fires immediately on lead creation (the <60s promise) ---
    textback = _responder.opening_textback(caller_name)
    send_simulated_sms(lead, textback)

    textback_dt = parse_ts(lead["timeline"][-1]["timestamp"])
    delta = (textback_dt - missed_dt).total_seconds()
    lead["textback"] = {
        "sent_at": lead["timeline"][-1]["timestamp"],
        "delta_seconds": delta,
        "under_60_seconds": delta < 60,
        "body": textback,
        "simulated": True,
    }
    # Surface the delta as its own timeline event so it is provable at a glance.
    add_event(lead, "textback_proof",
              f"SIMULATED textback sent {delta:.3f}s after the missed call "
              f"(target: <60s) — {'PASS' if delta < 60 else 'FAIL'}")

    with _lock:
        leads = load_leads()
        leads.append(lead)
        save_leads(leads)
    return lead


def handle_reply(payload):
    message = str(payload.get("message") or "").strip()
    if not message:
        raise ValueError("message is required")
    with _lock:
        leads = load_leads()
        lead = None
        if payload.get("lead_id"):
            lead = next((l for l in leads if l["id"] == payload["lead_id"]), None)
        elif payload.get("caller_number"):
            lead = next((l for l in reversed(leads)
                         if l["caller_number"] == str(payload["caller_number"])), None)
        if lead is None:
            raise KeyError("lead not found")

        if lead["status"] == "booked":
            add_event(lead, "inbound_sms_SIMULATED", message)
            send_simulated_sms(lead, "You're already booked — we'll see you then! (SIMULATED)")
            save_leads(leads)
            return lead

        add_event(lead, "inbound_sms_SIMULATED", message)
        stage = lead["stage"]
        q = lead.setdefault("qualification", {})

        if stage == "name":
            q["name"] = message
        elif stage == "job_type":
            q["job_type"] = message
        elif stage == "location":
            q["location"] = message
        elif stage == "timeline":
            q["timeline"] = message
            lead["path"] = detect_urgency(message)
            lead["qualification"]["urgency"] = lead["path"]
        elif stage == "budget":
            q["budget_range"] = message

        text, next_stage = generate_reply(stage, lead)
        lead["stage"] = next_stage
        send_simulated_sms(lead, text)

        if stage == "budget" and next_stage == "done":
            lead["status"] = "qualified"
            lead["qualification"]["outcome"] = (
                f"Qualified — {lead.get('path', 'planned')} path "
                f"({q.get('job_type', 'job')} in {q.get('location', 'unknown location')}, "
                f"budget {q.get('budget_range', 'unspecified')})"
            )
            add_event(lead, "qualification_complete", lead["qualification"]["outcome"])
            booking = book_next_slot(lead)
            if booking:
                lead["status"] = "booked"

        save_leads(leads)
        return lead


# --------------------------------------------------------------------------
# HTML (dashboard + lead page)
# --------------------------------------------------------------------------

BANNER = "SIMULATED DEMO — no real messages are sent. All telephony is SIMULATED."


def page(title, body):
    return (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem}"
        ".banner{background:#fff3cd;border:1px solid #e0a800;padding:.6rem 1rem;font-weight:700}"
        ".msg-out{background:#e8f5e9}.msg-in{background:#e3f2fd}"
        "div.ev{border-left:3px solid #888;margin:.4rem 0;padding:.3rem .6rem}"
        "code{background:#f0f0f0;padding:.1rem .3rem}</style>"
        f"<div class='banner'>{BANNER}</div>{body}"
    ).encode("utf-8")


def render_dashboard():
    leads = load_leads()
    if not leads:
        rows = "<p>No leads yet. POST a SIMULATED missed call to <code>/missed-call</code> to start.</p>"
    else:
        items = []
        for lead in reversed(leads):
            tb = lead.get("textback") or {}
            delta = tb.get("delta_seconds")
            delta_txt = f"textback in {delta:.3f}s" if isinstance(delta, (int, float)) else "no textback"
            booking = (lead.get("booking") or {}).get("slot_label", "not booked")
            items.append(
                f"<li><a href='/lead/{lead['id']}'><code>{lead['id']}</code></a> — "
                f"{html.escape(lead['caller_number'])} — status: {html.escape(lead['status'])} — "
                f"{delta_txt} — booking: {html.escape(str(booking))} [SIMULATED]</li>"
            )
        rows = "<ul>" + "".join(items) + "</ul>"
    return page("Peridot 60-Second System — SIMULATED demo",
                f"<h1>Peridot 60-Second System</h1><p>{BANNER}</p><h2>Leads</h2>{rows}")


def render_lead(lead_id):
    lead = next((l for l in load_leads() if l["id"] == lead_id), None)
    if lead is None:
        return None
    tb = lead.get("textback") or {}
    events = "".join(
        f"<div class='ev'><code>{html.escape(e['timestamp'])}</code> "
        f"<b>{html.escape(e['kind'])}</b> — {html.escape(e['text'])}</div>"
        for e in lead["timeline"]
    )
    thread = "".join(
        f"<div class='ev {'msg-in' if e['kind'].startswith('inbound') else 'msg-out'}'>"
        f"<code>{html.escape(e['timestamp'])}</code> "
        f"<b>{'Customer (SIMULATED)' if e['kind'].startswith('inbound') else 'Peridot (SIMULATED)'}:</b> "
        f"{html.escape(e['text'])}</div>"
        for e in lead["thread"]
        if e["kind"] in ("inbound_sms_SIMULATED", "textback_or_sms_sent_SIMULATED")
    )
    delta = tb.get("delta_seconds")
    proof = (
        f"<p><b>Textback proof:</b> missed call at <code>{html.escape(lead['timeline'][0]['timestamp'])}</code>, "
        f"SIMULATED textback at <code>{html.escape(str(tb.get('sent_at')))}</code> — "
        f"<b>delta: {delta:.3f} seconds</b> (target &lt;60s: "
        f"{'PASS' if isinstance(delta, (int, float)) and delta < 60 else 'FAIL'})</p>"
        if delta is not None else ""
    )
    body = (
        f"<h1>Lead {html.escape(lead['id'])}</h1>"
        f"<p>Caller: {html.escape(lead['caller_number'])} — Status: <b>{html.escape(lead['status'])}</b> "
        f"— Path: {html.escape(str(lead.get('path') or 'not yet determined'))}</p>"
        f"{proof}"
        f"<h2>Qualification</h2><pre>{html.escape(json.dumps(lead.get('qualification', {}), indent=2))}</pre>"
        f"<h2>Booking</h2><pre>{html.escape(json.dumps(lead.get('booking'), indent=2))}</pre>"
        f"<h2>Timeline (every step, timestamped)</h2>{events}"
        f"<h2>SIMULATED thread view</h2>{thread}"
        f"<p><a href='/'>&larr; all leads</a></p>"
    )
    return page(f"Lead {lead_id} — SIMULATED", body)


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep test output clean
        print(f"[SIMULATED demo] {self.address_string()} {fmt % args}")

    def _send(self, code, body, content_type="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, render_dashboard(), "text/html; charset=utf-8")
        elif path.startswith("/lead/"):
            out = render_lead(path[len("/lead/"):])
            if out is None:
                self._send(404, {"error": "lead not found"})
            else:
                self._send(200, out, "text/html; charset=utf-8")
        elif path == "/api/leads":
            self._send(200, {"simulated": True, "leads": load_leads()})
        elif path.startswith("/api/leads/"):
            lead = next((l for l in load_leads() if l["id"] == path[len("/api/leads/"):]), None)
            self._send(200 if lead else 404, lead or {"error": "lead not found"})
        elif path == "/api/slots":
            self._send(200, load_json(SLOTS_FILE, {"slots": []}))
        elif path == "/health":
            self._send(200, {"ok": True, "simulated": True, "llm_backend": LLM_BACKEND})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/missed-call":
                lead = create_lead(payload)
                print(f"[SIMULATED] missed-call -> lead {lead['id']}, "
                      f"textback delta {lead['textback']['delta_seconds']:.3f}s (SIMULATED SMS, not sent)")
                self._send(201, lead)
            elif path == "/reply":
                lead = handle_reply(payload)
                self._send(200, lead)
            else:
                self._send(404, {"error": "not found"})
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except KeyError as exc:
            self._send(404, {"error": str(exc)})
        except Exception as exc:  # demo: surface the error, don't hang
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})


def main():
    port = int(os.environ.get("PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Peridot 60-Second System — SIMULATED DEMO (no real messages are sent)")
    print(f"Listening on http://127.0.0.1:{port}/  LLM_BACKEND={LLM_BACKEND}  slots={SLOTS_FILE}")
    server.serve_forever()


if __name__ == "__main__":
    main()
