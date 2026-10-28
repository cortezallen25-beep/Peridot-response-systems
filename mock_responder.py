"""
Deterministic mock responder — default LLM backend for the demo.

SIMULATED: no model, no network, no API key. Same inputs -> same outputs,
so the end-to-end test is reproducible.

It drives a scripted qualification:
    name -> job type -> location -> timeline/urgency -> budget range

Urgency branching (visibly different paths, per the approved sprint plan):
  * URGENT path  -- timeline text mentions urgent / asap / emergency /
    today / burst / leak / flooding / no water / no heat, etc.
    Reply acknowledges the emergency, promises priority handling, and the
    lead is flagged `urgency="urgent"` / `path="urgent"` so booking picks
    the earliest slot and marks it PRIORITY.
  * PLANNED path -- anything else. Reply talks about scheduling a proper
    estimate visit, lead is flagged `urgency="planned"` / `path="planned"`.
"""

URGENT_KEYWORDS = (
    "urgent", "asap", "emergency", "today", "right now", "burst",
    "leak", "flooding", "flood", "no water", "no heat", "no hot water",
    "broken pipe", "sewage", "sparking",
)

# Stage order. The textback (sent on missed call) asks the first question.
STAGES = ["name", "job_type", "location", "timeline", "budget", "done"]

QUESTIONS = {
    "name": "First up — what's your name?",
    "job_type": "Thanks, {name}. What kind of job is it? (e.g. burst pipe, bathroom remodel, fence repair)",
    "location": "Got it. Where's the job located? (neighborhood or ZIP is fine)",
    "timeline": "When do you need it done? If it's urgent — leaking, no water, safety issue — say URGENT. Otherwise tell me your ideal timeline.",
    "budget": "Last one: what's your budget range for this? (e.g. $500-$1,000, or 'not sure yet')",
}


def detect_urgency(timeline_text):
    t = (timeline_text or "").lower()
    return "urgent" if any(k in t for k in URGENT_KEYWORDS) else "planned"


class MockResponder:
    name = "mock"

    def opening_textback(self, caller_name=None):
        greeting = f"Hi {caller_name}, " if caller_name else "Hi there, "
        return (
            greeting
            + "sorry we missed your call! This is the (SIMULATED) textback from "
              "CLA Peridot — we can get you booked right here by text. "
            + QUESTIONS["name"]
        )

    def reply_for_completed_stage(self, stage, lead):
        """Return the next outbound (SIMULATED) message after `stage` was answered.

        `lead` is the lead dict with lead['qualification'] partially filled.
        Returns (text, next_stage).
        """
        q = lead.get("qualification", {})
        if stage == "name":
            return QUESTIONS["job_type"].format(name=q.get("name", "there")), "job_type"
        if stage == "job_type":
            return QUESTIONS["location"], "location"
        if stage == "location":
            return QUESTIONS["timeline"], "timeline"
        if stage == "timeline":
            if lead.get("path") == "urgent":
                text = (
                    "URGENT PATH: Understood — that sounds urgent, so I'm flagging this "
                    "as a priority and we'll aim for the earliest slot we have. "
                    + QUESTIONS["budget"]
                )
            else:
                text = (
                    "PLANNED PATH: No rush — we'll line up a proper estimate visit "
                    "that fits your timeline. " + QUESTIONS["budget"]
                )
            return text, "budget"
        if stage == "budget":
            if lead.get("path") == "urgent":
                text = (
                    "URGENT PATH: Thanks — you're qualified and flagged PRIORITY. "
                    "Booking you into our earliest open slot now…"
                )
            else:
                text = (
                    "PLANNED PATH: Thanks — you're qualified. "
                    "Booking your estimate visit into the next open slot now…"
                )
            return text, "done"
        return "Thanks! We'll be in touch. (SIMULATED)", "done"
