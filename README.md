# Peridot "60-Second System" — Hackathon Demo

**SIMULATED DEMO — no real messages are sent.** All telephony is
SIMULATED: a "missed call" is a JSON POST, a "textback" is an entry in a
local outbox log and a web thread view. No carrier, no Twilio, no real
SMS, no real calls, no paid services, and **no API key is required to run
this demo**. Built for Cortez Allen / CLA Peridot LLC — Meta Global AI
Developer Hackathon, Startup Track, Oct 26–Nov 4, 2026.

Demo flow: contractor misses a call → AI textback fires in under 60
seconds → AI qualification conversation by (simulated) text → appointment
booked on a calendar → every step written into one customer record with
timestamps.

Stack: Python 3.12 standard library only (`http.server`). Flask/FastAPI
were not installed on the host and nothing was pip-installed.

## Run the service

```bash
cd ~/workspace/hackathon/demo
python3 app.py            # http://127.0.0.1:8765/
PORT=9000 python3 app.py  # custom port
```

Open http://127.0.0.1:8765/ for the dashboard (lists leads). Click a lead
for its full timestamped timeline, qualification, booking, the textback
delta proof, and the SIMULATED thread view.

Drive it by hand (all SIMULATED):

```bash
# 1. Fake missed call (simulated carrier webhook)
curl -s -X POST localhost:8765/missed-call -H 'Content-Type: application/json' \
  -d '{"caller_number":"+13145550199","caller_name":"Maria","timestamp":"2026-10-20T12:00:00+00:00"}'

# 2. Simulated inbound reply — repeat, answering each question
curl -s -X POST localhost:8765/reply -H 'Content-Type: application/json' \
  -d '{"lead_id":"<id from step 1>","message":"Burst pipe — URGENT, water everywhere"}'
```

Qualification script: name → job type → location → timeline/urgency →
budget range. A timeline mentioning urgent/ASAP/emergency/leak/etc. takes
the **URGENT PATH** (priority flag, earliest slot); anything else takes
the **PLANNED PATH** (estimate-visit flow). The paths are labelled in the
thread so the difference is visible on stage.

Endpoints: `GET /`, `GET /lead/<id>`, `GET /api/leads`,
`GET /api/leads/<id>`, `GET /api/slots`, `GET /health`,
`POST /missed-call`, `POST /reply`.

Local state (all in this folder, all SIMULATED): `leads.json` (customer
records), `outbox.log` (one JSON line per simulated SMS), `slots.json`
(slot store).

## Run the test

```bash
cd ~/workspace/hackathon/demo
python3 e2e_test.py
```

Starts the service on a scratch port with scratch data files, fires a
fake missed call, asserts the textback delta is <60s from timestamps,
runs both the URGENT and PLANNED conversations to qualification, asserts
the booking landed and the slot is marked taken, and checks the
dashboard/lead pages. Prints PASS/FAIL per stage and an overall verdict.
Last full run: `e2e-test-output.txt` in this folder.

## Where the Meta API key goes

The default backend is the deterministic mock responder
(`mock_responder.py`) — no key needed. The Meta Model API backend lives
in `meta_client.py` (OpenAI-compatible chat client structure):

```bash
export LLM_BACKEND=meta
export META_MODEL_API_KEY=<your key>     # env var only — never in code/files
export META_MODEL_API_BASE=<base URL>    # optional — default https://api.meta.ai/v1
export META_MODEL_NAME=<model name>      # optional — default muse-spark-1.3
python3 app.py
```

Endpoint details landed and were verified live on Oct 23, 2026:
`meta_client.py` defaults to base `https://api.meta.ai/v1` and model
`muse-spark-1.3`. In meta mode the real model rephrases the scripted
replies (facts, questions, and path labels preserved; the state machine
stays deterministic). If the Meta client is not configured or a call
fails, generation falls back to the mock responder.

## Where the Google Calendar hook goes

In `app.py`, function `book_next_slot()` — search for the banner comment
`GOOGLE CALENDAR HOOK — REAL INTEGRATION GOES HERE`. Today it picks the
next open slot from the local `slots.json` store and marks it taken. The
real integration replaces only that lookup: read free/busy from Google
Calendar, create the event, and store its ID in
`lead["booking"]["calendar_event_id"]` (already present, currently
`null`). Everything downstream already consumes `lead["booking"]`.

## Honesty note

Telephony is SIMULATED and is labelled "SIMULATED" in the UI, the logs,
the API records, and the test output. Do not present this demo as sending
real texts. Swapping in a real telephony provider is a production step
outside this hackathon scaffold's scope.
