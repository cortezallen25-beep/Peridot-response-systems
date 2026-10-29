#!/usr/bin/env python3
"""
Peridot "60-Second System" — API tooling (post-demo build, Oct 29, 2026).

Runs the proven 60-Second pipeline end to end against a lead:

    intake -> textback -> qualification exchange -> booking proposal
           -> follow-up drafts (day 3 / 7 / 14)

The flow, stage order, questions, and urgency logic are the FROZEN demo's:
this module imports MockResponder / detect_urgency read-only from
the repo root's mock_responder.py, so the tooling behaves
exactly like the submitted demo (whose live path, verified Oct 23, 2026,
rephrases the scripted stage text through the real Meta model with the
system prompt copied verbatim from app.py generate_reply()).

Backends (one interface, --backend):
    wonder      Meta Model API, model muse-wonder-1.0.
                Spends the $150 hackathon credit on Cortez's account.
                Auth: META_MODEL_API_KEY environment variable (the pattern
                used by meta_client.py) — never hard-coded, printed,
                stored in files, or committed.
    spark-open  Muse Spark Open (open weights) via Groq's hosted endpoint.
                Auth: GROQ_API_KEY environment variable.
                The model is discovered from Groq's live /models list; if
                Groq is not serving Spark Open, the run stops and says so.
                --spark-open-model MODEL overrides discovery (used to
                verify the Groq transport with a stand-in model; the
                transcript labels it as such).
    mock        No network. Scripted demo text only. $0.

SIMULATED telephony, same as the demo: no real SMS is sent, no real call
is made. Outbound messages are printed and saved to a run transcript.

Every live call appends a row to usage-ledger.md (sidecar totals in
usage-ledger.json). Wonder spend is tracked cumulatively against the
$150 credit.

Usage:
    python3 peridot_pipeline.py --backend wonder --scenario urgent
    python3 peridot_pipeline.py --backend spark-open --scenario planned
    python3 peridot_pipeline.py --backend mock --scenario urgent
    python3 peridot_pipeline.py --backend wonder --lead-json my_lead.json
    python3 peridot_pipeline.py --backend spark-open --scenario planned \
        --spark-open-model qwen/qwen3.8-27b     # transport verification

Lead JSON shape:
    {"caller_name": null, "caller_number": "+1-314-555-0000",
     "replies": ["<name>", "<job type>", "<location>", "<timeline>",
                 "<budget>"]}
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

# --- Credentials (meta_client.py pattern: environment variables only) ---
# Live backends read their API key from the environment at call time:
# META_MODEL_API_KEY (wonder / Meta Model API) and GROQ_API_KEY
# (spark-open / Groq). Keys are never hard-coded, printed, or committed.


def _env_key(name):
    return os.environ.get(name, "").strip()


def _read_json_response(resp):
    return json.loads(resp.read().decode("utf-8"))

# --- Frozen demo flow (read-only import; the demo files are untouched) ---
# This file lives in tooling/; the frozen demo's mock_responder.py sits at
# the repo root, one level up.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from mock_responder import MockResponder, detect_urgency  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
LEDGER_MD = BASE_DIR / "usage-ledger.md"
LEDGER_JSON = BASE_DIR / "usage-ledger.json"
STATE_SLOTS = BASE_DIR / "state" / "slots.json"
DEMO_SLOTS = REPO_ROOT / "slots.json"
RUNS_DIR = BASE_DIR / "runs"

CREDIT_USD = 150.00

# Pricing, USD per 1M tokens (Meta Model API, researched Oct 29, 2026).
PRICING = {
    "muse-wonder-1.0": {"in": 4.00, "cached_in": 0.20, "out": 20.00},
    "muse-spark-1.3": {"in": 1.25, "cached_in": 0.15, "out": 4.25},
}
GROQ_FREE_MODELS_COST = 0.0  # Groq free tier: no charge; tokens still logged.

# Copied verbatim from demo/app.py generate_reply() — the prompt the frozen
# demo's verified live path used to rephrase scripted stage text.
REPHRASE_SYSTEM = (
    "You are the text-message voice of CLA Peridot, a "
    "contracting company, inside a SIMULATED demo. "
    "Rewrite the given outbound message in a natural, "
    "warm texting tone. Keep every fact and the exact "
    "question being asked. Keep the markers 'URGENT "
    "PATH:' / 'PLANNED PATH:' and any '(SIMULATED)' "
    "label exactly as written if present. Change "
    "nothing else about the meaning. Reply with the "
    "rewritten message only."
)

FOLLOWUP_SYSTEM = (
    "You are the text-message voice of CLA Peridot, a contracting company. "
    "A lead was qualified and booked for an estimate visit but has not yet "
    "confirmed or paid anything. Write the Peridot follow-up sequence for "
    "this lead: a Day 3 check-in text, a Day 7 follow-up text, and a "
    "Day 14 final follow-up text. Each text: 1-3 short sentences, warm and "
    "direct, texts like a real person, references the lead's actual job and "
    "booked slot, gives one clear next step (reply to confirm the visit). "
    "No hype, no discounts, no invented facts. Label each draft 'DAY 3:', "
    "'DAY 7:', 'DAY 14:' on its own line, with the message after the label."
)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class BackendError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def cost_for(model, usage):
    """Estimated USD cost for one call from its usage block."""
    if usage is None:
        return None
    tin = usage.get("prompt_tokens") or 0
    tout = usage.get("completion_tokens") or 0
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    price = PRICING.get(model)
    if price is None:
        return GROQ_FREE_MODELS_COST
    uncached = max(tin - cached, 0)
    return (
        uncached * price["in"] / 1_000_000
        + cached * price["cached_in"] / 1_000_000
        + tout * price["out"] / 1_000_000
    )


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------

LEDGER_HEADER = (
    "# Peridot API tooling — usage ledger\n\n"
    "Every live API call made by `peridot_pipeline.py` is appended "
    "here.\nWonder calls spend the $150 hackathon credit on the Meta "
    "Model API.\nSpark-Open calls run on Groq's free tier ($0).\n\n"
    "| UTC timestamp | Run | Backend | Model | Stage | Tokens in | "
    "Tokens out | Cached in | Est. cost (USD) |\n"
    "|---|---|---|---|---|---|---|---|---|\n"
)


def ensure_ledger_md():
    if not LEDGER_MD.exists():
        LEDGER_MD.write_text(LEDGER_HEADER, encoding="utf-8")


def load_totals():
    if LEDGER_JSON.exists():
        try:
            return json.loads(LEDGER_JSON.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"credit_usd": CREDIT_USD, "backends": {}}


def record_call(totals, run_id, backend_name, model, stage, usage):
    cost = cost_for(model, usage)
    tin = (usage or {}).get("prompt_tokens")
    tout = (usage or {}).get("completion_tokens")
    cached = ((usage or {}).get("prompt_tokens_details") or {}).get("cached_tokens")
    b = totals["backends"].setdefault(
        backend_name, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    )
    b["calls"] += 1
    b["tokens_in"] += tin or 0
    b["tokens_out"] += tout or 0
    b["cost_usd"] += cost or 0.0
    LEDGER_JSON.write_text(json.dumps(totals, indent=2), encoding="utf-8")

    ensure_ledger_md()
    if cost is None:
        row = (f"| {now_iso()} | {run_id} | {backend_name} | {model} "
               f"| {stage} | n/a | n/a | n/a | n/a |\n")
    else:
        row = (f"| {now_iso()} | {run_id} | {backend_name} | {model} "
               f"| {stage} "
               f"| {tin if tin is not None else 'n/a'} "
               f"| {tout if tout is not None else 'n/a'} "
               f"| {cached if cached is not None else 'n/a'} "
               f"| {cost:.6f} |\n")
    with LEDGER_MD.open("a", encoding="utf-8") as fh:
        fh.write(row)
    return cost


def update_ledger_summary(totals):
    ensure_ledger_md()
    wonder = totals["backends"].get("wonder", {"calls": 0, "cost_usd": 0.0})
    spent = wonder["cost_usd"]
    block = (
        "<!-- WONDER-BURN-START -->\n"
        "## Wonder credit burn (auto-updated each run)\n\n"
        f"- Credit: ${totals.get('credit_usd', CREDIT_USD):.2f} "
        "(Meta Model API hackathon credit)\n"
        f"- Wonder calls logged: {wonder['calls']}\n"
        f"- Estimated spend to date: ${spent:.4f}\n"
        f"- Estimated remaining: ${totals.get('credit_usd', CREDIT_USD) - spent:.4f}\n"
        "<!-- WONDER-BURN-END -->\n"
    )
    text = LEDGER_MD.read_text(encoding="utf-8")
    # Remove any existing block wherever it sits, then re-insert it
    # immediately before the calls table so the table stays contiguous
    # as new rows are appended at end of file.
    if "<!-- WONDER-BURN-START -->" in text:
        head = text.split("<!-- WONDER-BURN-START -->")[0]
        tail = text.split("<!-- WONDER-BURN-END -->", 1)[1].lstrip("\n")
        text = (head.rstrip("\n") + "\n\n" + tail) if tail else head
    marker = "| UTC timestamp |"
    if marker in text:
        idx = text.index(marker)
        text = text[:idx] + block + "\n" + text[idx:]
    else:
        text = text.rstrip("\n") + "\n\n" + block
    LEDGER_MD.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------

class OpenAICompatBackend:
    name = "base"
    model = None

    def chat(self, messages, stage, ledger_ctx, temperature=0.3):
        raise NotImplementedError


class WonderBackend(OpenAICompatBackend):
    name = "wonder"
    model = "muse-wonder-1.0"
    URL = "https://api.meta.ai/v1/chat/completions"

    def chat(self, messages, stage, ledger_ctx, temperature=0.3):
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature}
        api_key = _env_key("META_MODEL_API_KEY")
        if not api_key:
            raise BackendError(
                "META_MODEL_API_KEY is not set; the wonder backend needs "
                "the Meta Model API key in the environment (never in code)."
            )
        req = urllib.request.Request(
            self.URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        opener = urllib.request.build_opener(NoRedirect())
        try:
            with opener.open(req, timeout=90) as resp:
                data = _read_json_response(resp)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            raise BackendError(f"Meta Model API HTTP {exc.code}: {body}") from exc
        except Exception as exc:
            raise BackendError(f"Meta Model API call failed: {exc}") from exc
        return self._finish(data, stage, ledger_ctx)

    def _finish(self, data, stage, ledger_ctx):
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise BackendError(f"Unexpected response shape: {str(data)[:400]}") from exc
        usage = data.get("usage")
        cost = record_call(ledger_ctx["totals"], ledger_ctx["run_id"],
                           self.name, self.model, stage, usage)
        return text, usage, cost


class SparkOpenBackend(OpenAICompatBackend):
    name = "spark-open"
    URL = "https://api.groq.com/openai/v1/chat/completions"
    MODELS_URL = "https://api.groq.com/openai/v1/models"

    def __init__(self, model_override=None):
        self.model_override = model_override
        self.model = None
        self.stand_in = False

    def _api_key(self):
        key = _env_key("GROQ_API_KEY")
        if not key:
            raise BackendError(
                "GROQ_API_KEY is not set; the spark-open backend needs "
                "the Groq API key in the environment (never in code)."
            )
        return key

    def list_models(self):
        req = urllib.request.Request(
            self.MODELS_URL,
            headers={"Authorization": f"Bearer {self._api_key()}",
                     "User-Agent": BROWSER_UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = _read_json_response(resp)
        except Exception as exc:
            raise BackendError(f"Groq /models call failed: {exc}") from exc
        return [m.get("id", "") for m in data.get("data", [])]

    def resolve_model(self):
        if self.model_override:
            self.model = self.model_override
            self.stand_in = "spark" not in self.model_override.lower()
            return self.model
        ids = self.list_models()
        for mid in ids:
            low = mid.lower()
            if "spark" in low and "muse" in low:
                self.model = mid
                return self.model
        for mid in ids:
            if "spark" in mid.lower():
                self.model = mid
                return self.model
        raise BackendError(
            "Muse Spark Open is NOT served by Groq on this account. "
            f"Live Groq model list ({len(ids)} models): {', '.join(ids)}. "
            "No paid hosting was signed up for (per build instructions); "
            "the route is wired and will work unchanged once a host "
            "(Groq/Together/Fireworks/OpenRouter) serves the model on an "
            "account Cortez controls."
        )

    def chat(self, messages, stage, ledger_ctx, temperature=0.3):
        if self.model is None:
            self.resolve_model()
        # Groq free tier enforces a 1000 output-tokens/min expectation cap
        # on qwen3.8-27b (seen live Oct 29, 2026: max_tokens=1024 -> 429).
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "max_tokens": 800}
        req = urllib.request.Request(
            self.URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._api_key()}",
                     "User-Agent": BROWSER_UA},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = _read_json_response(resp)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            raise BackendError(f"Groq HTTP {exc.code}: {body}") from exc
        except Exception as exc:
            raise BackendError(f"Groq call failed: {exc}") from exc
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise BackendError(f"Unexpected Groq response shape: {str(data)[:400]}") from exc
        usage = data.get("usage")
        cost = record_call(ledger_ctx["totals"], ledger_ctx["run_id"],
                           self.name, self.model, stage, usage)
        return text, usage, cost


class MockBackend(OpenAICompatBackend):
    name = "mock"
    model = "mock (no API call)"

    def chat(self, messages, stage, ledger_ctx, temperature=0.3):
        # Never called for rephrases in mock mode (pipeline short-circuits);
        # present only to satisfy the interface.
        raise BackendError("mock backend makes no API calls")


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

SCENARIOS = {
    "urgent": {
        "caller_name": None,
        "caller_number": "+1-314-555-0117",
        "replies": [
            "Dana Whitfield",
            "Burst pipe — water leaking under my kitchen sink",
            "Ferguson, 63135",
            "URGENT — it's leaking right now, today if possible",
            "$500 to $1,500",
        ],
    },
    "planned": {
        "caller_name": None,
        "caller_number": "+1-314-555-0143",
        "replies": [
            "Marcus Bell",
            "Fence repair — a section of my fence blew down",
            "St. Louis, 63136",
            "Next two weeks, no rush",
            "Not sure yet",
        ],
    },
}

STAGE_FIELDS = {
    "name": "name",
    "job_type": "job_type",
    "location": "location",
    "timeline": "timeline",
    "budget": "budget_range",
}


def ensure_slots(fresh=False):
    STATE_SLOTS.parent.mkdir(parents=True, exist_ok=True)
    if fresh or not STATE_SLOTS.exists():
        STATE_SLOTS.write_text(DEMO_SLOTS.read_text(encoding="utf-8"),
                               encoding="utf-8")
    return json.loads(STATE_SLOTS.read_text(encoding="utf-8"))


def book_next_slot(lead, store):
    """Same wording/logic as the demo's book_next_slot, against the
    tooling's own scratch slot store (the frozen demo's file is untouched)."""
    for slot in store.get("slots", []):
        if not slot.get("taken"):
            slot["taken"] = True
            slot["booked_by"] = lead["id"]
            STATE_SLOTS.write_text(json.dumps(store, indent=2), encoding="utf-8")
            priority = lead.get("path") == "urgent"
            lead["booking"] = {
                "slot_id": slot["id"],
                "slot_label": slot["label"],
                "slot_start": slot["start"],
                "priority": priority,
                "simulated": True,
            }
            confirm = (
                f"You're booked: {slot['label']}"
                + (" — PRIORITY slot for your urgent job." if priority
                   else " for your estimate visit.")
                + " We'll send a reminder before then. "
                  "(SIMULATED message — no real SMS was sent.)"
            )
            return confirm
    return None


def run_pipeline(backend, lead_spec, fresh_state=False):
    run_id = "run-" + uuid.uuid4().hex[:8]
    totals = load_totals()
    ledger_ctx = {"totals": totals, "run_id": run_id}
    responder = MockResponder()
    live = backend.name != "mock"

    lead = {
        "id": "lead-" + uuid.uuid4().hex[:8],
        "simulated": True,
        "caller_number": lead_spec["caller_number"],
        "caller_name": lead_spec.get("caller_name"),
        "status": "qualifying",
        "stage": "name",
        "path": None,
        "qualification": {},
        "booking": None,
        "backend": backend.name,
        "model": getattr(backend, "model", None),
        "created_at": now_iso(),
        "thread": [],
    }
    run_cost = 0.0
    run_calls = 0

    def outbound(stage_label, scripted):
        """Demo live-path pattern: scripted text, model rephrases it live."""
        nonlocal run_cost, run_calls
        if not live:
            lead["thread"].append({"dir": "out", "stage": stage_label,
                                   "text": scripted, "source": "scripted"})
            return scripted
        text, usage, cost = backend.chat(
            [{"role": "system", "content": REPHRASE_SYSTEM},
             {"role": "user", "content": scripted}],
            stage_label, ledger_ctx,
        )
        run_calls += 1
        run_cost += cost or 0.0
        final = (text or "").strip() or scripted
        lead["thread"].append({"dir": "out", "stage": stage_label,
                               "text": final, "source": "model",
                               "scripted": scripted,
                               "usage": usage, "cost_usd": cost})
        return final

    # 1) Intake + textback (fires immediately — the <60s promise).
    scripted_textback = responder.opening_textback(lead["caller_name"])
    textback = outbound("textback", scripted_textback)
    lead["textback"] = {"body": textback, "simulated": True}

    # 2) Qualification exchange, demo stage order.
    for reply in lead_spec["replies"]:
        stage = lead["stage"]
        lead["thread"].append({"dir": "in", "stage": stage, "text": reply})
        field = STAGE_FIELDS[stage]
        lead["qualification"][field] = reply
        if stage == "timeline":
            lead["path"] = detect_urgency(reply)
            lead["qualification"]["urgency"] = lead["path"]
        scripted, next_stage = responder.reply_for_completed_stage(stage, lead)
        outbound(f"reply_after_{stage}", scripted)
        lead["stage"] = next_stage
        if next_stage == "done":
            break

    # 3) Booking proposal.
    booking_confirm = None
    if lead["stage"] == "done":
        lead["status"] = "qualified"
        q = lead["qualification"]
        lead["qualification"]["outcome"] = (
            f"Qualified — {lead.get('path', 'planned')} path "
            f"({q.get('job_type', 'job')} in {q.get('location', 'unknown location')}, "
            f"budget {q.get('budget_range', 'unspecified')})"
        )
        store = ensure_slots(fresh=fresh_state)
        scripted_confirm = book_next_slot(lead, store)
        if scripted_confirm:
            booking_confirm = outbound("booking_confirm", scripted_confirm)
            lead["status"] = "booked"

    # 4) Follow-up drafts (Day 3 / 7 / 14).
    followups = None
    if live:
        q = lead["qualification"]
        summary = (
            f"Lead: {q.get('name', 'unknown')} — job: {q.get('job_type', 'n/a')}; "
            f"location: {q.get('location', 'n/a')}; "
            f"timeline: {q.get('timeline', 'n/a')} ({lead.get('path', 'planned')} path); "
            f"budget: {q.get('budget_range', 'n/a')}; "
            f"booked slot: {(lead.get('booking') or {}).get('slot_label', 'none')}."
        )
        text, usage, cost = backend.chat(
            [{"role": "system", "content": FOLLOWUP_SYSTEM},
             {"role": "user", "content": summary}],
            "followup_drafts", ledger_ctx,
        )
        run_calls += 1
        run_cost += cost or 0.0
        followups = (text or "").strip()
        lead["thread"].append({"dir": "draft", "stage": "followup_drafts",
                                "text": followups, "source": "model",
                                "usage": usage, "cost_usd": cost})
    else:
        followups = "(mock backend — follow-up drafting is a live-model stage)"

    update_ledger_summary(totals)

    result = {
        "run_id": run_id,
        "backend": backend.name,
        "model": getattr(backend, "model", None),
        "stand_in_model": bool(getattr(backend, "stand_in", False)),
        "lead": lead,
        "booking_confirm": booking_confirm,
        "followups": followups,
        "live_calls": run_calls,
        "run_cost_usd": run_cost,
        "finished_at": now_iso(),
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RUNS_DIR / f"{stamp}-{backend.name}-{run_id}.json"
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["transcript_path"] = str(out_path)
    return result


def print_result(result):
    lead = result["lead"]
    print(f"\n=== Peridot 60-Second System — {result['backend']} backend "
          f"(model: {result['model']}) ===")
    if result.get("stand_in_model"):
        print("NOTE: stand-in model — Groq transport verification only; "
              "this run did NOT use Muse Spark Open.")
    print(f"Lead {lead['id']} — {lead['caller_number']} — "
          f"status: {lead['status']} — path: {lead.get('path')}")
    for msg in lead["thread"]:
        who = {"out": "Peridot", "in": "Lead   ", "draft": "Draft  "}[msg["dir"]]
        print(f"[{who}] ({msg['stage']}) {msg['text']}")
    if result["booking_confirm"]:
        print(f"\nBooking: {json.dumps(lead['booking'])}")
    print(f"\nFollow-up drafts:\n{result['followups']}")
    print(f"\nLive calls: {result['live_calls']} — run cost: "
          f"${result['run_cost_usd']:.4f}")
    print(f"Transcript: {result['transcript_path']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--backend", required=True,
                    choices=["wonder", "spark-open", "mock"])
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="urgent")
    ap.add_argument("--lead-json", help="path to a lead JSON file "
                                        "(overrides --scenario)")
    ap.add_argument("--spark-open-model", default=None,
                    help="override Spark Open model discovery on Groq")
    ap.add_argument("--fresh-state", action="store_true",
                    help="reset the scratch slot store from the demo's "
                         "slots.json before booking")
    args = ap.parse_args(argv)

    if args.lead_json:
        lead_spec = json.loads(Path(args.lead_json).read_text(encoding="utf-8"))
    else:
        lead_spec = SCENARIOS[args.scenario]

    if args.backend == "wonder":
        backend = WonderBackend()
    elif args.backend == "spark-open":
        backend = SparkOpenBackend(model_override=args.spark_open_model)
        try:
            backend.resolve_model()
        except BackendError as exc:
            print(f"SPARK-OPEN ROUTE UNAVAILABLE: {exc}", file=sys.stderr)
            return 3
        print(f"Spark-Open route: Groq model '{backend.model}'"
              + (" (STAND-IN — transport verification)"
                 if backend.stand_in else ""))
    else:
        backend = MockBackend()

    try:
        result = run_pipeline(backend, lead_spec, fresh_state=args.fresh_state)
    except BackendError as exc:
        print(f"PIPELINE FAILED ({args.backend}): {exc}", file=sys.stderr)
        return 1
    print_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
