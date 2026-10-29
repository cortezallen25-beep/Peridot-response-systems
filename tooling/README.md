# Peridot 60-Second System — API tooling

Built Oct 29, 2026, after the hackathon demo was frozen, and added to
this repo the same day. This folder is **not** part of the frozen demo —
the demo files at the repo root are unchanged. It runs the same
60-Second pipeline as the demo — intake → textback → qualification
exchange → booking proposal → follow-up drafts (day 3 / 7 / 14) — as a
CLI against real model APIs.

The flow, stage order, questions, urgency keywords, and the rephrase
system prompt are the frozen demo's own: `peridot_pipeline.py` imports
`MockResponder` / `detect_urgency` read-only from the repo root's
`mock_responder.py` and uses the exact rephrase prompt from `app.py`.
Telephony stays SIMULATED — no real SMS is ever sent; outbound messages
print to the terminal and save to a transcript in `runs/`.

## Backends

| Backend | Model | Route | Cost |
|---|---|---|---|
| `wonder` | `muse-wonder-1.0` | Meta Model API (`api.meta.ai/v1`), key via `META_MODEL_API_KEY` env var | Spends the $150 hackathon credit ($4 in / $20 out per 1M tokens) |
| `spark-open` | Muse Spark Open (open weights) | Groq hosted endpoint, key via `GROQ_API_KEY` env var | Groq free tier, $0 |
| `mock` | — | No network; scripted demo text | $0 |

API keys are read from environment variables at call time — the same
pattern as the demo's `meta_client.py`. Keys are never hard-coded,
printed, stored in files, or committed.

**Spark Open status (Oct 29, 2026):** Groq does **not** currently serve
Muse Spark Open on the author's account (live `/models` list checked;
11 models, none is Spark Open). The backend discovers the model from
the live list at runtime, so it will work unchanged the moment a host
the author controls serves it. No paid hosting (Together/Fireworks/
OpenRouter) was signed up for. To verify the Groq transport in the
meantime, override with `--spark-open-model qwen/qwen3.8-27b` —
transcripts label this as a stand-in run, never as Spark Open output.
Note: Groq's free tier caps expected output at ~1000 tokens/min on
qwen3.8-27b, so the backend sends `max_tokens: 800`.

## Run it

```bash
cd tooling

# Live, on the $150 credit (Wonder):
export META_MODEL_API_KEY=<your key>     # env var only — never in code/files
python3 peridot_pipeline.py --backend wonder --scenario urgent

# Spark Open route (stops with a clear message until a host serves it):
export GROQ_API_KEY=<your key>           # env var only — never in code/files
python3 peridot_pipeline.py --backend spark-open --scenario planned

# Groq transport verification with a stand-in model:
python3 peridot_pipeline.py --backend spark-open --scenario planned \
    --spark-open-model qwen/qwen3.8-27b

# No network, scripted flow only:
python3 peridot_pipeline.py --backend mock --scenario urgent

# Your own scripted lead:
python3 peridot_pipeline.py --backend wonder --lead-json lead.json
# lead.json: {"caller_name": null, "caller_number": "+1-...",
#             "replies": ["<name>", "<job type>", "<location>",
#                         "<timeline>", "<budget>"]}
```

Built-in scenarios are invented test leads (555 numbers): `urgent` =
Dana Whitfield, burst pipe in Ferguson; `planned` = Marcus Bell, fence
repair in St. Louis. Add `--fresh-state` to reset the scratch booking
slots (`state/slots.json`) from the repo root `slots.json` — the
tooling never writes the demo's own files.

## Usage ledger

- `usage-ledger.md` — one row per live call (timestamp, run, backend,
  model, stage, tokens in/out, cached tokens, estimated cost) plus an
  auto-updated **Wonder credit burn** block: calls logged, estimated
  spend to date, estimated remaining of the $150.
- `usage-ledger.json` — the same totals in machine-readable form.
- Wonder estimates use the published rates ($4.00 in / $0.20 cached in /
  $20.00 out per 1M). Token counts come from each API response's usage
  block. The balance is an estimate from our own call log — the API
  exposes no balance endpoint to this tooling, so treat the ledger as
  the tracking source, not Meta's billing system.

First live day (Oct 29, 2026): one full Wonder pipeline run = 8 calls,
1,177 in / 4,816 out tokens, **$0.1010** — about 1,480 full pipeline
runs per $150 at that rate. Wonder's completion-token counts run high
for short rephrases (reasoning tokens are included); the follow-up
draft call is the most expensive stage (~$0.039). The transcript of
that run is saved as submission evidence in
`runs/20261029T170532Z-wonder-run-d5419743.json` (invented test lead
only).
