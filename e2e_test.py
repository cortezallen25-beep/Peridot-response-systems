#!/usr/bin/env python3
"""
End-to-end test for the Peridot "60-Second System" SIMULATED demo.

SIMULATED throughout: no real SMS, no real calls, no paid services,
no API key. Starts the service on a scratch port with a scratch data
directory (so the real slots.json / leads.json are untouched), then:

  1. fires a fake (SIMULATED) missed call
  2. asserts a lead record was created
  3. asserts the SIMULATED textback fired with delta < 60 seconds
  4. runs a full SIMULATED qualification conversation (URGENT path)
  5. runs a second conversation on the PLANNED path and asserts the
     paths visibly differ
  6. asserts qualification outcome + booking landed in the record and
     the booked slot is marked taken in the slot store
  7. asserts the dashboard / lead page shows the SIMULATED label and
     the textback delta

Prints PASS/FAIL per stage and an overall verdict. Exit code 0 = PASS.

Run:  python3 e2e_test.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PORT = 18765
BASE = f"http://127.0.0.1:{PORT}"

results = []


def check(stage, ok, detail=""):
    results.append((stage, ok))
    print(f"{'PASS' if ok else 'FAIL'}: {stage}" + (f" — {detail}" if detail else ""))


def http(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            return resp.status, (json.loads(body) if resp.headers.get_content_type() == "application/json" else body)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, body


def wait_for_server(proc, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            status, _ = http("GET", "/health")
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def run_conversation(lead_id, answers):
    lead = None
    for msg in answers:
        status, lead = http("POST", "/reply", {"lead_id": lead_id, "message": msg})
        assert status == 200, f"reply failed: {status} {lead}"
    return lead


def main():
    print("=== Peridot 60-Second System — SIMULATED end-to-end test ===")
    print("Telephony is SIMULATED: no real SMS, no real calls.")

    tmp = tempfile.TemporaryDirectory(prefix="peridot-demo-e2e-")
    tmpdir = Path(tmp.name)
    # Scratch slot store: copy of the real one so bookings don't consume it.
    slots_src = json.loads((BASE_DIR / "slots.json").read_text())
    (tmpdir / "slots.json").write_text(json.dumps(slots_src))
    env = {
        **os.environ,
        "PORT": str(PORT),
        "LLM_BACKEND": "mock",
        "DEMO_SLOTS_FILE": str(tmpdir / "slots.json"),
        "DEMO_LEADS_FILE": str(tmpdir / "leads.json"),
        "DEMO_OUTBOX_FILE": str(tmpdir / "outbox.log"),
    }
    proc = subprocess.Popen(
        [sys.executable, str(BASE_DIR / "app.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        ok = wait_for_server(proc)
        check("service starts (stdlib http.server, no API key)", ok)
        if not ok:
            return finish(proc, tmp)

        # --- Stage 1: SIMULATED missed call -------------------------------
        missed_ts = datetime.now(timezone.utc).isoformat()
        status, lead = http("POST", "/missed-call", {
            "caller_number": "+13145550199",
            "caller_name": "Test Caller",
            "timestamp": missed_ts,
        })
        check("SIMULATED missed call accepted / lead created",
              status == 201 and bool(lead.get("id")),
              f"status={status} lead_id={lead.get('id')}")
        lead_id = lead["id"]

        # --- Stage 2: textback < 60s, provable from timestamps ------------
        tb = lead.get("textback") or {}
        delta = tb.get("delta_seconds")
        check("SIMULATED textback fired on lead creation",
              bool(tb.get("body")) and "SIMULATED" in tb.get("body", ""))
        check("textback delta < 60 seconds",
              isinstance(delta, (int, float)) and delta < 60,
              f"delta={delta}s (missed {missed_ts} -> textback {tb.get('sent_at')})")
        kinds = [e["kind"] for e in lead["timeline"]]
        check("timeline records missed_call + textback events",
              "missed_call_SIMULATED" in kinds and "textback_or_sms_sent_SIMULATED" in kinds,
              f"kinds={kinds}")
        outbox = (tmpdir / "outbox.log").read_text() if (tmpdir / "outbox.log").exists() else ""
        check("SIMULATED textback appended to outbox log",
              lead_id in outbox and "SIMULATED" in outbox)

        # --- Stage 3: full qualification conversation, URGENT path --------
        lead = run_conversation(lead_id, [
            "Maria Gonzalez",
            "Burst pipe in the kitchen",
            "Ferguson, 63135",
            "URGENT — water is leaking everywhere, need someone today",
            "$500-$1,500",
        ])
        q = lead.get("qualification") or {}
        check("qualification conversation completed (all fields captured)",
              all(q.get(k) for k in ("name", "job_type", "location", "timeline", "budget_range")),
              f"qualification={q}")
        check("URGENT path taken for urgent job",
              lead.get("path") == "urgent" and q.get("urgency") == "urgent",
              f"path={lead.get('path')}")
        thread_text = " ".join(e["text"] for e in lead["thread"])
        check("URGENT path is visibly labelled in the thread",
              "URGENT PATH" in thread_text)
        inbound = [e for e in lead["timeline"] if e["kind"] == "inbound_sms_SIMULATED"]
        check("every conversation step is timestamped in the record",
              len(inbound) == 5 and all(e.get("timestamp") for e in lead["timeline"]),
              f"inbound_messages={len(inbound)} timeline_events={len(lead['timeline'])}")

        # --- Stage 4: booking ---------------------------------------------
        booking = lead.get("booking") or {}
        check("qualification outcome recorded",
              bool(q.get("outcome")) and lead.get("status") == "booked",
              f"status={lead.get('status')} outcome={q.get('outcome')}")
        check("appointment booked from local slot store",
              bool(booking.get("slot_id")) and bool(booking.get("slot_label")),
              f"booking={booking}")
        status, slots = http("GET", "/api/slots")
        slot = next((s for s in slots["slots"] if s["id"] == booking.get("slot_id")), None)
        check("booked slot marked taken in the slot store",
              bool(slot) and slot.get("taken") is True and slot.get("booked_by") == lead_id,
              f"slot={slot}")

        # --- Stage 5: PLANNED path differs --------------------------------
        status, lead2 = http("POST", "/missed-call", {
            "caller_number": "+13145550188",
            "caller_name": "Planned Caller",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        lead2 = run_conversation(lead2["id"], [
            "James Lee",
            "Bathroom remodel",
            "St. Louis, 63110",
            "Planning ahead — sometime next month is fine",
            "$5,000-$10,000",
        ])
        thread2 = " ".join(e["text"] for e in lead2["thread"])
        check("PLANNED path taken for a planned project",
              lead2.get("path") == "planned" and "PLANNED PATH" in thread2,
              f"path={lead2.get('path')}")
        check("urgent and planned paths book different slots",
              (lead2.get("booking") or {}).get("slot_id") != booking.get("slot_id"),
              f"urgent_slot={booking.get('slot_id')} planned_slot={(lead2.get('booking') or {}).get('slot_id')}")

        # --- Stage 6: record + dashboard via API/HTML ----------------------
        status, fetched = http("GET", f"/api/leads/{lead_id}")
        check("full customer record retrievable (GET /api/leads/<id>)",
              status == 200 and fetched.get("booking", {}).get("slot_id") == booking.get("slot_id"))
        req = urllib.request.Request(BASE + f"/lead/{lead_id}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            lead_page = resp.read().decode()
        check("lead page shows SIMULATED label + textback delta proof",
              "SIMULATED DEMO" in lead_page and "delta:" in lead_page and "PASS" in lead_page)
        req = urllib.request.Request(BASE + "/")
        with urllib.request.urlopen(req, timeout=10) as resp:
            dashboard = resp.read().decode()
        check("dashboard lists leads with SIMULATED label",
              "SIMULATED DEMO" in dashboard and lead_id in dashboard)
    finally:
        finish_server(proc)
        tmp.cleanup()

    return report()


def finish_server(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def finish(proc, tmp):
    finish_server(proc)
    tmp.cleanup()
    return report()


def report():
    passed = sum(1 for _, ok in results if ok)
    overall = passed == len(results) and len(results) > 0
    print(f"=== {passed}/{len(results)} stages passed ===")
    print(f"OVERALL: {'PASS' if overall else 'FAIL'} (SIMULATED demo e2e)")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
