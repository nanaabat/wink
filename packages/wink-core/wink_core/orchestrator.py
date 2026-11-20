from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from tw_contracts import validate
from tw_scene import find_joint, find_object, scene_hash

from .audit import AuditLog
from .policy import Policy

SOURCE_LABEL = {"user_rule": "your rule", "inferred_preference": "inferred from your feedback",
                "model_guess": "model guess", "manual": "you entered this goal"}


class WinkError(RuntimeError):
    pass


class PolicyViolation(WinkError):
    pass


def _guard_text(text: str) -> str:
    try:
        from hunch_adapter import assert_no_emotion_claims
    except ImportError:  # wink runs without the adapter installed; apply the same rule locally
        import re
        if re.search(r"\b(detect|read)s?\b.{0,30}\b(your|their)\b.{0,20}\bemotions?\b", text.lower()):
            raise WinkError("unsupported emotional-detection claim")
        return text
    return assert_no_emotion_claims(text)


@dataclass
class Plan:
    id: str
    state: str
    goal: dict | None = None
    observations: list = field(default_factory=list)
    skill: dict | None = None
    target: dict | None = None
    simulation: dict | None = None
    preview: dict | None = None
    explanation: list = field(default_factory=list)
    receipt: dict | None = None
    suggestions: list = field(default_factory=list)
    scene_hash: str | None = None
    approval_key: str | None = None
    pending_moves: list = field(default_factory=list)
    error: str | None = None


class Wink:
    """Coordinator. hunch and tinker are optional: no Hunch -> manual goals; no Tinker -> suggestions, no action."""

    def __init__(self, *, connector, audit: AuditLog, hunch=None, tinker=None, policy: Policy | None = None, sun_dir=None):
        self.connector, self.audit, self.hunch, self.tinker = connector, audit, hunch, tinker
        self.policy = policy or Policy()
        self.sun_dir = sun_dir
        self.plans: dict[str, Plan] = {}
        self._receipts: dict[str, dict] = {}

    # ---------------- pipeline up to the approval gate
    def plan(self, *, context: dict | None = None, preferences: list[dict] | None = None, manual_goal: dict | None = None) -> Plan:
        p = Plan(id="plan-" + uuid.uuid4().hex[:12], state="idle")
        self.plans[p.id] = p
        scene = self.connector.get_scene() if self.connector else None
        p.scene_hash = scene_hash(scene) if scene else None
        self.audit.append("plan.start", {"plan_id": p.id, "hunch": getattr(self.hunch, "name", None),
                                         "tinker": self.tinker is not None, "scene_hash": p.scene_hash})
        # observe + propose
        if manual_goal is not None:
            goal = dict(manual_goal)
            goal.setdefault("schema_version", "1.0.0")
            goal.setdefault("id", "goal-" + uuid.uuid4().hex[:12])
            goal["source"] = "manual"
            goal.setdefault("provider", {"name": "user", "mock": False})
            goal.setdefault("rationale", "goal entered by you")
            goal.setdefault("uncertainty", 0.0)
            goal.setdefault("user_preferences_used", [])
            goal.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            p.goal = validate("GoalProposal", goal)
        elif self.hunch is not None:
            p.observations = self.hunch.observe(context or {})
            p.state = "observed"
            p.goal = validate("GoalProposal", self.hunch.propose_goal(p.observations, preferences or []))
        else:
            return self._fail(p, "no Hunch provider is configured: enter a goal manually", state="needs_manual_goal")
        p.state = "proposed"
        _guard_text(p.goal["rationale"])
        self.audit.append("goal.proposed", {"plan_id": p.id, "goal": p.goal})
        if p.goal["uncertainty"] > self.policy.max_uncertainty:
            p.explanation = self._explain(p)
            return self._fail(p, f"proposal uncertainty {p.goal['uncertainty']:.2f} is above the policy limit "
                                 f"{self.policy.max_uncertainty:.2f}; confirm or enter the goal yourself", state="low_confidence")
        if self.tinker is None:
            p.suggestions = [f"adjust the {p.goal['target_state']['object_type']} ({p.goal['target_state']['intent']})"]
            p.state = "suggestion_only"
            p.explanation = self._explain(p) + ["tinker is not connected, so wink can only suggest; no physical action is possible"]
            self.audit.append("plan.suggestion_only", {"plan_id": p.id})
            return p
        # choose skill
        skills = self.tinker.list_skills(scene, p.goal)
        skills = [s for s in skills if s["evaluation"]["confidence"] >= self.policy.min_skill_confidence]
        if not skills:
            return self._fail(p, "no compatible tinker skill with enough confidence: demonstrate one first", state="no_skill")
        p.skill = skills[0]
        p.state = "skill_chosen"
        # constraint check (static) + target resolution
        try:
            oid, jn, val, why = self.tinker.resolve_target(scene, p.goal, p.skill, sun_dir=self.sun_dir,
                                                           max_sunlit=self.policy.max_sunlit_fraction)
        except Exception as e:
            return self._fail(p, f"cannot resolve the goal in this scene: {e}")
        obj = find_object(scene, oid)
        j = find_joint(obj, jn)
        p.target = {"object_id": oid, "joint": jn, "from": j["value"], "to": val, **why}
        p.state = "constraints_checked"
        # simulate
        sim = self.tinker.simulate(p.skill, scene, object_id=oid, joint=jn, goal_value=val, sun_dir=self.sun_dir)
        p.simulation = sim
        p.state = "simulated"
        self.audit.append("simulated", {"plan_id": p.id, "simulation_id": sim["id"], "success": sim["success"],
                                        "violations": [v["code"] for v in sim["violations"]]})
        p.explanation = self._explain(p)
        if not sim["success"]:
            return self._fail(p, "simulation failed: " + "; ".join(f"{v['code']}: {v['message']}" for v in sim["violations"]), state="simulation_failed")
        p.preview = self.tinker.preview(sim)
        p.state = "awaiting_approval"
        self.audit.append("awaiting_approval", {"plan_id": p.id})
        return p

    def _explain(self, p: Plan) -> list[str]:
        g = p.goal
        lines = []
        if g:
            prov = g["provider"]
            lines.append(f"goal: {g['target_state']['intent'].replace('_', ' ')} "
                         f"(source: {SOURCE_LABEL[g['source']]}; provider: {prov['name']}{' [mock]' if prov['mock'] else ''}; "
                         f"uncertainty {g['uncertainty']:.0%})")
            lines.append("why: " + g["rationale"])
        for o in p.observations:
            val = round(o["value"], 3) if isinstance(o["value"], float) else o["value"]
            text = o["label"] if o.get("label") else f"{o['kind']} = {val}"
            if o["kind"] == "daylight":
                text += f" ({val:.0%})"
            lines.append(f"signal: {text} (from {o['source']}{', mock' if o.get('mock') else ''})")
        if p.skill:
            ev = p.skill["evaluation"]
            lines.append(f"skill: {p.skill['name']} v{p.skill['version']} ({p.skill['model_ref']['kind'].replace('_', ' ')}), "
                         f"confidence {ev['confidence']:.2f} (heuristic, not a calibrated probability), "
                         f"learned from {ev['demonstrations']} demonstrations")
        if p.target:
            lines.append(f"action: move {p.target['object_id']}.{p.target['joint']} from {p.target['from']:.3f} to {p.target['to']:.3f}"
                         + (f" ({p.target['reason']})" if p.target.get("reason") else ""))
        if p.simulation:
            m = p.simulation["metrics"]
            ok = "passed" if p.simulation["success"] else "failed"
            line = f"simulation {ok} in {m.get('engine', '?')}: goal error {m.get('goal_error', 0):.4f}, max speed {m.get('max_speed', 0):.3f}"
            if "sunlit_fraction_start" in m:
                line += f", direct sun on window {m['sunlit_fraction_start']:.0%} -> {m['sunlit_fraction_end']:.0%}"
            lines.append(line)
            lines.append("simulation is a sandbox check, not the host's geometry engine and not a real building")
        return [_guard_text(x) for x in lines]

    def _fail(self, p: Plan, msg: str, state: str = "failed") -> Plan:
        p.state, p.error = state, msg
        self.audit.append("plan." + state, {"plan_id": p.id, "error": msg})
        return p

    # ---------------- approval gate
    def reject(self, plan_id: str, user_id: str, reason: str = "") -> Plan:
        p = self._get(plan_id)
        if p.state != "awaiting_approval":
            raise WinkError(f"cannot reject a plan in state {p.state}")
        if p.preview and p.preview.get("handle"):
            self.connector.clear_preview(p.preview["handle"])
        p.state = "rejected"
        self.audit.append("rejected", {"plan_id": p.id, "user_id": user_id, "reason": reason[:500]})
        return p

    def cancel(self, plan_id: str) -> Plan:
        p = self._get(plan_id)
        if p.state in ("applied", "verified", "recorded"):
            raise WinkError("already applied: use undo")
        if p.preview and p.preview.get("handle"):
            self.connector.clear_preview(p.preview["handle"])
        p.state = "cancelled"
        self.audit.append("cancelled", {"plan_id": p.id})
        return p

    def _check_approval(self, p: Plan, approval: dict, current_scene: dict) -> dict:
        if p.state != "awaiting_approval":
            raise WinkError(f"plan is {p.state}, not awaiting approval")
        approval = validate("ActionApproval", approval)
        if approval["proposal_id"] != p.goal["id"] or approval["simulation_id"] != p.simulation["id"]:
            raise PolicyViolation("approval does not match this plan's proposal and simulation")
        ok, why = self.policy.scope_allowed(approval["scope"])
        if not ok:
            self.audit.append("approval.refused", {"plan_id": p.id, "reason": why})
            raise PolicyViolation(why)
        approved_at = datetime.fromisoformat(approval["approved_at"])
        if datetime.now(timezone.utc) - approved_at > timedelta(seconds=self.policy.approval_ttl_s):
            raise PolicyViolation("approval expired")
        if scene_hash(current_scene) != p.scene_hash:
            p.state = "stale"
            self.audit.append("plan.stale", {"plan_id": p.id})
            raise PolicyViolation("the scene changed since simulation: re-run the plan")
        self.audit.append("approved", {"plan_id": p.id, "approval": approval})
        return approval

    def approve(self, plan_id: str, approval: dict, current_scene: dict) -> dict:
        """Two-step flow for hosts that apply edits themselves (e.g. over IPC). Returns the moves to apply;
        the host applies them as one undo step and then calls confirm_applied()."""
        p = self._get(plan_id)
        key = f"{approval.get('proposal_id')}|{approval.get('simulation_id')}|{approval.get('user_id')}"
        if p.approval_key == key and p.state in ("approved_pending_apply", "recorded", "rolled_back", "failed"):
            return {"plan_id": p.id, "moves": p.pending_moves, "state": p.state, "receipt": p.receipt}
        approval = self._check_approval(p, approval, current_scene)
        p.approval_key = key
        if approval["scope"] == "sandbox_preview":
            p.state = "previewed_only"
            return {"plan_id": p.id, "moves": [], "state": p.state}
        p.pending_moves = [{"object_id": p.target["object_id"], "joint": p.target["joint"], "value": p.target["to"]}]
        p.state = "approved_pending_apply"
        return {"plan_id": p.id, "moves": p.pending_moves, "state": p.state}

    def confirm_applied(self, plan_id: str, after_scene: dict, host_undo_ref: str | None, error: str | None = None) -> dict:
        p = self._get(plan_id)
        if p.state != "approved_pending_apply":
            if p.receipt is not None:
                return p.receipt
            raise WinkError(f"plan is {p.state}, nothing pending")
        key = f"{p.target['object_id']}.{p.target['joint']}"
        before = {key: p.target["from"]}
        result, after = "applied", before
        if error:
            result, p.error = "failed", error[:500]
        else:
            try:
                v = find_joint(find_object(after_scene, p.target["object_id"]), p.target["joint"])["value"]
                after = {key: v}
                if abs(v - p.target["to"]) > 1e-6:
                    result, p.error = "partial_rolled_back", f"verification failed: host reports {v:.4f}, expected {p.target['to']:.4f}"
            except KeyError as e:
                result, p.error = "partial_rolled_back", f"verification failed: {e}"
        action_id = "act-" + uuid.uuid4().hex[:12]
        audit_id = self.audit.append("action." + result, {"plan_id": p.id, "action_id": action_id, "before": before, "after": after,
                                                          "undo_ref": host_undo_ref, "error": p.error})
        p.receipt = validate("ActionReceipt", {"schema_version": "1.0.0", "action_id": action_id, "before_state": before,
                                               "after_state": after if result == "applied" else before,
                                               "host_undo_ref": host_undo_ref if result == "applied" else None,
                                               "result": result, "audit_id": audit_id})
        p.state = {"applied": "recorded", "failed": "failed", "partial_rolled_back": "rolled_back"}[result]
        return p.receipt

    def approve_and_apply(self, plan_id: str, approval: dict) -> dict:
        """In-process flow: approval gate -> tinker.apply through the connector -> verify -> receipt.
        Rolls back through host undo if verification fails."""
        p = self._get(plan_id)
        key = f"{approval.get('proposal_id')}|{approval.get('simulation_id')}|{approval.get('user_id')}"
        if p.approval_key == key and p.receipt is not None:
            return p.receipt  # repeated request: idempotent, no second apply
        out = self.approve(plan_id, approval, self.connector.get_scene())
        if out["state"] == "previewed_only":
            return {}
        undo_ref, err = None, None
        try:
            undo_ref = self.tinker.apply({"simulation_id": p.simulation["id"], "approval": approval})
        except Exception as e:  # host failed mid-apply; connector rolled back its partial step
            err = f"host apply failed: {e}"
        receipt = self.confirm_applied(plan_id, self.connector.get_scene(), undo_ref, err)
        if receipt["result"] == "partial_rolled_back" and undo_ref is not None:
            self.connector.undo(undo_ref)
            self.audit.append("action.rollback_done", {"plan_id": p.id, "undo_ref": undo_ref})
        if p.preview and p.preview.get("handle"):
            self.connector.clear_preview(p.preview["handle"])
        return receipt

    def undo(self, plan_id: str) -> dict:
        p = self._get(plan_id)
        if not p.receipt or not p.receipt["host_undo_ref"]:
            raise WinkError("nothing to undo")
        self.connector.undo(p.receipt["host_undo_ref"])
        p.state = "undone"
        aid = self.audit.append("action.undone", {"plan_id": p.id, "action_id": p.receipt["action_id"]})
        return {"plan_id": p.id, "audit_id": aid}

    def feedback(self, plan_id: str, rating: int, note: str = "") -> None:
        p = self._get(plan_id)
        fb = {"plan_id": p.id, "rating": int(max(-1, min(1, rating))), "note": note[:500],
              "intent": "calmer" if p.goal and p.goal["target_state"]["intent"] == "reduce_direct_sun" else "other"}
        self.audit.append("feedback", fb)
        if self.hunch is not None:
            self.hunch.record_feedback(fb)

    def _get(self, plan_id: str) -> Plan:
        if plan_id not in self.plans:
            raise WinkError("unknown plan")
        return self.plans[plan_id]
