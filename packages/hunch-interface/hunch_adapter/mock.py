from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .guard import assert_no_emotion_claims


def _now():
    return datetime.now(timezone.utc).isoformat()


class MockHunchProvider:
    """Deterministic stand-in for Hunch. Labels every output as mock. Rules:
    - a user rule matching the context wins (source: user_rule)
    - else a preference inferred from recorded feedback (source: inferred_preference)
    - else a model guess from daylight alone (source: model_guess), with high uncertainty
    """
    name = "hunch-mock"
    is_mock = True

    def __init__(self, *, sun_threshold: float = 0.2):
        self.sun_threshold = sun_threshold
        self.feedback: list[dict] = []

    def observe(self, context: dict) -> list[dict]:
        obs = [{"kind": "time_of_day", "value": context.get("time_of_day", "unknown"), "source": "user context",
                "provider": self.name, "mock": True}]
        if "sunlit_fraction" in context:
            f = float(context["sunlit_fraction"])
            obs.append({"kind": "daylight", "value": f, "label": "strong direct sun" if f > self.sun_threshold else "soft light",
                        "source": "geometry: share of the window reached by direct sun", "provider": self.name, "mock": True})
            atm = "energized" if f > self.sun_threshold else "calm"
            obs.append({"kind": "predicted_atmosphere", "value": atm, "label": f"space predicted to read as {atm}",
                        "source": "mock model guess from daylight only", "provider": self.name, "mock": True})
        return obs

    def _inferred(self):
        likes = [f for f in self.feedback if f.get("intent") == "calmer" and f.get("rating", 0) > 0]
        return len(likes) >= 2

    def propose_goal(self, observations: list[dict], preferences: list[dict]) -> dict:
        tod = next((o["value"] for o in observations if o["kind"] == "time_of_day"), "unknown")
        sun = next((o["value"] for o in observations if o["kind"] == "daylight"), None)
        rule = next((p for p in preferences if p.get("kind") == "user_rule" and p.get("when") in (tod, "any")), None)
        if rule is not None:
            source, unc, used = "user_rule", 0.1, [rule["id"]]
            intent = rule.get("intent", "calmer")
            why = f"your rule '{rule.get('text', rule['id'])}' applies in the {tod}"
        elif self._inferred():
            source, unc, used, intent = "inferred_preference", 0.35, ["feedback-history"], "calmer"
            why = "you approved calmer settings in this situation before"
        else:
            source, unc, used, intent = "model_guess", 0.6, [], "calmer"
            why = "no rule or feedback yet; guess from daylight only"
        if sun is not None:
            why += f"; direct sun reaches {sun:.0%} of the window"
        target = {"object_type": "blind", "object_id": None, "joint": "tilt", "target_value": None,
                  "intent": "reduce_direct_sun" if intent == "calmer" else "increase_daylight"}
        rationale = assert_no_emotion_claims(f"make the room calmer: {why}. this is a prediction about the space, from a mock provider.")
        return {"schema_version": "1.0.0", "id": "goal-" + uuid.uuid4().hex[:12], "source": source,
                "provider": {"name": self.name, "mock": True}, "target_state": target, "rationale": rationale,
                "uncertainty": unc, "user_preferences_used": used, "created_at": _now()}

    def record_feedback(self, feedback: dict) -> None:
        self.feedback.append(dict(feedback))
