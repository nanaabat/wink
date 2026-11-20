import json
from datetime import datetime, timedelta, timezone

import pytest

from hunch_adapter import MockHunchProvider
from tinker_service import TinkerProvider
from tw_connector import MockHost
from tw_fixtures import BLIND_ID, room_scene
from tw_sim import sunlit_fraction
from wink_core import AuditIntegrityError, AuditLog, Policy, PolicyViolation, Wink, WinkError

SUN = [-0.55, -0.7, 0.45]
HI = 1.4486
RULE = {"id": "rule-1", "kind": "user_rule", "when": "afternoon", "intent": "calmer", "text": "calmer in afternoon sun"}


def ctx(host):
    return {"time_of_day": "afternoon", "sunlit_fraction": sunlit_fraction(host.get_scene(), SUN)}


def make(trained, tmp_path, *, hunch=True, tinker=True, host=None, policy=None):
    reg, _ = trained
    host = host or MockHost(room_scene(blind_value=HI))
    w = Wink(connector=host, audit=AuditLog(tmp_path / "audit.jsonl"), hunch=MockHunchProvider() if hunch else None,
             tinker=TinkerProvider(reg, host) if tinker else None, policy=policy, sun_dir=SUN)
    return w, host


def approval(p, scope="host_document", when=None, user="tester"):
    return {"schema_version": "1.0.0", "proposal_id": p.goal["id"], "simulation_id": p.simulation["id"], "user_id": user,
            "approved_at": (when or datetime.now(timezone.utc)).isoformat(), "scope": scope}


def blind(host):
    return next(o for o in host.get_scene()["objects"] if o["id"] == BLIND_ID)["joints"][0]["value"]


def test_end_to_end_with_mock_hunch(trained, tmp_path):
    w, host = make(trained, tmp_path)
    p = w.plan(context=ctx(host), preferences=[RULE])
    assert p.state == "awaiting_approval", p.error
    text = "\n".join(p.explanation)
    assert "your rule" in text and "[mock]" in text and "simulation passed" in text and "->" in text
    assert blind(host) == pytest.approx(HI)  # nothing moved before approval
    r = w.approve_and_apply(p.id, approval(p))
    assert r["result"] == "applied" and blind(host) == pytest.approx(p.target["to"])
    assert p.state == "recorded"
    w.undo(p.id)
    assert blind(host) == pytest.approx(HI)
    w.feedback(p.id, 1, "nice")
    kinds = [e["kind"] for e in w.audit.verify()]
    assert kinds[0] == "plan.start" and "approved" in kinds and "action.applied" in kinds and kinds[-1] == "feedback"


def test_hunch_missing_manual_goal(trained, tmp_path):
    w, host = make(trained, tmp_path, hunch=False)
    p = w.plan(context=ctx(host))
    assert p.state == "needs_manual_goal"
    p2 = w.plan(manual_goal={"target_state": {"object_type": "blind", "object_id": BLIND_ID, "joint": "tilt",
                                              "target_value": 0.5, "intent": "reduce_direct_sun"}})
    assert p2.state == "awaiting_approval" and p2.goal["source"] == "manual"
    assert "you entered this goal" in p2.explanation[0]


def test_tinker_missing_suggests_only(trained, tmp_path):
    w, host = make(trained, tmp_path, tinker=False)
    p = w.plan(context=ctx(host), preferences=[RULE])
    assert p.state == "suggestion_only" and p.suggestions and blind(host) == pytest.approx(HI)
    with pytest.raises(WinkError):
        w.approve_and_apply(p.id, {"proposal_id": p.goal["id"], "simulation_id": "x", "user_id": "u"})


def test_low_confidence_proposal_blocked(trained, tmp_path):
    w, host = make(trained, tmp_path)
    p = w.plan(context=ctx(host), preferences=[])  # model guess, uncertainty 0.6
    assert p.state == "low_confidence" and "uncertainty" in p.error


def test_rejected_approval(trained, tmp_path):
    w, host = make(trained, tmp_path)
    p = w.plan(context=ctx(host), preferences=[RULE])
    w.reject(p.id, "tester", "not now")
    assert p.state == "rejected" and blind(host) == pytest.approx(HI)
    assert p.preview["handle"] not in host.previews
    with pytest.raises(WinkError):
        w.approve_and_apply(p.id, approval(p))


def test_stale_scene(trained, tmp_path):
    w, host = make(trained, tmp_path)
    p = w.plan(context=ctx(host), preferences=[RULE])
    host.user_set_joint(BLIND_ID, "tilt", 1.0)  # someone moved it after simulation
    with pytest.raises(PolicyViolation, match="scene changed"):
        w.approve_and_apply(p.id, approval(p))
    assert p.state == "stale"


def test_repeated_request_is_idempotent(trained, tmp_path):
    w, host = make(trained, tmp_path)
    p = w.plan(context=ctx(host), preferences=[RULE])
    a = approval(p)
    r1 = w.approve_and_apply(p.id, a)
    r2 = w.approve_and_apply(p.id, a)
    assert r1 == r2 and host.applied_count == 1


def test_cancellation(trained, tmp_path):
    w, host = make(trained, tmp_path)
    p = w.plan(context=ctx(host), preferences=[RULE])
    w.cancel(p.id)
    assert p.state == "cancelled"
    with pytest.raises(WinkError):
        w.approve_and_apply(p.id, approval(p))


def test_failure_after_partial_apply_rolls_back(trained, tmp_path):
    host = MockHost(room_scene(blind_value=HI))
    w, _ = make(trained, tmp_path, host=host)
    p = w.plan(context=ctx(host), preferences=[RULE])
    real_get = host.get_scene
    calls = {"n": 0}

    def lying_get():  # host reports a different value after apply -> verification fails
        s = real_get()
        calls["n"] += 1
        if calls["n"] >= 2:  # 1st call: stale check, 2nd: post-apply verification
            for o in s["objects"]:
                if o["id"] == BLIND_ID:
                    o["joints"][0]["value"] = 0.123
        return s
    host.get_scene = lying_get
    r = w.approve_and_apply(p.id, approval(p))
    host.get_scene = real_get
    assert r["result"] == "partial_rolled_back" and p.state == "rolled_back"
    assert blind(host) == pytest.approx(HI)


def test_host_failure_during_apply(trained, tmp_path):
    host = MockHost(room_scene(blind_value=HI), fail_apply_after=0)
    w, _ = make(trained, tmp_path, host=host)
    p = w.plan(context=ctx(host), preferences=[RULE])
    r = w.approve_and_apply(p.id, approval(p))
    assert r["result"] == "failed" and blind(host) == pytest.approx(HI)
    # recovery: a fresh plan on a healthy host works
    host.fail_apply_after = None
    p2 = w.plan(context=ctx(host), preferences=[RULE])
    assert w.approve_and_apply(p2.id, approval(p2))["result"] == "applied"


def test_hardware_scope_disabled_by_default(trained, tmp_path):
    w, host = make(trained, tmp_path)
    p = w.plan(context=ctx(host), preferences=[RULE])
    with pytest.raises(PolicyViolation, match="hardware"):
        w.approve_and_apply(p.id, approval(p, scope="hardware"))
    w.policy = Policy(hardware_enabled=True)
    with pytest.raises(PolicyViolation, match="safety controller"):
        w.approve_and_apply(p.id, approval(p, scope="hardware"))
    assert blind(host) == pytest.approx(HI)


def test_mismatched_or_expired_approval(trained, tmp_path):
    w, host = make(trained, tmp_path)
    p = w.plan(context=ctx(host), preferences=[RULE])
    bad = approval(p)
    bad["simulation_id"] = "sim-other"
    with pytest.raises(PolicyViolation):
        w.approve_and_apply(p.id, bad)
    with pytest.raises(PolicyViolation, match="expired"):
        w.approve_and_apply(p.id, approval(p, when=datetime.now(timezone.utc) - timedelta(hours=1)))


def test_sandbox_preview_scope_does_not_touch_document(trained, tmp_path):
    w, host = make(trained, tmp_path)
    p = w.plan(context=ctx(host), preferences=[RULE])
    w.approve_and_apply(p.id, approval(p, scope="sandbox_preview"))
    assert p.state == "previewed_only" and blind(host) == pytest.approx(HI)


def test_simulation_failure_stops_before_approval(trained, tmp_path):
    host = MockHost(room_scene(blind_value=HI, obstacle=([2.0, -0.9, 1.0], [2.0, 0.3, 0.4])))
    w, _ = make(trained, tmp_path, host=host)
    p = w.plan(context=ctx(host), preferences=[RULE])
    assert p.state == "simulation_failed" and "collision" in "\n".join(p.explanation + [p.error])


def test_audit_integrity_detects_tampering(trained, tmp_path):
    w, host = make(trained, tmp_path)
    p = w.plan(context=ctx(host), preferences=[RULE])
    w.approve_and_apply(p.id, approval(p))
    path = tmp_path / "audit.jsonl"
    lines = path.read_text().splitlines()
    assert len(w.audit.verify()) == len(lines)
    e = json.loads(lines[1])
    e["data"]["goal"]["rationale"] = "edited"
    lines[1] = json.dumps(e, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(AuditIntegrityError):
        AuditLog(path)
    path.write_text("\n".join(lines[:1] + lines[2:]) + "\n")  # deletion
    with pytest.raises(AuditIntegrityError):
        AuditLog(path)


def test_audit_survives_restart(tmp_path):
    a = AuditLog(tmp_path / "a.jsonl")
    a.append("x", {"n": 1})
    b = AuditLog(tmp_path / "a.jsonl")
    b.append("y", {"n": 2})
    assert [e["seq"] for e in b.verify()] == [1, 2]
