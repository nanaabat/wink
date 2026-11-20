import http.client
import json
import threading
from datetime import datetime, timezone

import pytest

from hunch_adapter import MockHunchProvider
from tw_connector.ipc import load_or_create_token
from tw_fixtures import BLIND_ID, room_scene
from tw_scene import find_joint, find_object, pose_for_value
from wink_core.http import WinkHTTPServer

SUN = [-0.55, -0.7, 0.45]
RULE = {"id": "rule-1", "kind": "user_rule", "when": "afternoon", "intent": "calmer", "text": "calmer in afternoon sun"}


@pytest.fixture()
def srv(trained, tmp_path):
    reg, _ = trained
    s = WinkHTTPServer(load_or_create_token(tmp_path / "tok"), tmp_path / "audit.jsonl", hunch=MockHunchProvider(),
                       tinker_registry=reg.root)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.shutdown()
    s.server_close()


def call(s, path, body=None, auth=True):
    c = http.client.HTTPConnection("127.0.0.1", s.server_address[1], timeout=60)
    h = {"Content-Type": "application/json"}
    if auth:
        h["Authorization"] = "Bearer " + s.token
    c.request("POST" if body is not None else "GET", path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    out = r.status, json.loads(r.read())
    c.close()
    return out


def test_requires_token(srv):
    assert call(srv, "/v1/health", auth=False)[0] == 401
    st, h = call(srv, "/v1/health")
    assert st == 200 and h["hunch"] == "hunch-mock" and h["hunch_mock"] and h["tinker"]


def test_two_step_flow_host_applies(srv):
    scene = room_scene(blind_value=1.4486)
    st, plan = call(srv, "/v1/plan", {"scene": scene, "context": {"time_of_day": "afternoon"}, "preferences": [RULE], "sun_dir": SUN})
    assert st == 200 and plan["state"] == "awaiting_approval", plan
    appr = {"schema_version": "1.0.0", "proposal_id": plan["goal"]["id"], "simulation_id": plan["simulation"]["id"],
            "user_id": "tester", "approved_at": datetime.now(timezone.utc).isoformat(), "scope": "host_document"}
    st, out = call(srv, "/v1/approve", {"plan_id": plan["plan_id"], "approval": appr, "scene": scene})
    assert st == 200 and out["state"] == "approved_pending_apply" and len(out["moves"]) == 1
    mv = out["moves"][0]
    o = find_object(scene, mv["object_id"])
    j = find_joint(o, mv["joint"])
    o["pose"] = pose_for_value(o, j, mv["value"])
    j["value"] = mv["value"]
    st, rc = call(srv, "/v1/confirm", {"plan_id": plan["plan_id"], "scene": scene, "host_undo_ref": "blender-undo-1"})
    assert st == 200 and rc["result"] == "applied"
    assert call(srv, "/v1/audit/verify")[1]["ok"]


def test_host_reports_wrong_value(srv):
    scene = room_scene(blind_value=1.4486)
    _, plan = call(srv, "/v1/plan", {"scene": scene, "context": {"time_of_day": "afternoon"}, "preferences": [RULE], "sun_dir": SUN})
    appr = {"schema_version": "1.0.0", "proposal_id": plan["goal"]["id"], "simulation_id": plan["simulation"]["id"],
            "user_id": "tester", "approved_at": datetime.now(timezone.utc).isoformat(), "scope": "host_document"}
    call(srv, "/v1/approve", {"plan_id": plan["plan_id"], "approval": appr, "scene": scene})
    st, rc = call(srv, "/v1/confirm", {"plan_id": plan["plan_id"], "scene": scene, "host_undo_ref": "u"})
    assert rc["result"] == "partial_rolled_back" and rc["host_undo_ref"] is None


def test_stale_and_bad_ids(srv):
    scene = room_scene(blind_value=1.4486)
    _, plan = call(srv, "/v1/plan", {"scene": scene, "context": {"time_of_day": "afternoon"}, "preferences": [RULE], "sun_dir": SUN})
    appr = {"schema_version": "1.0.0", "proposal_id": plan["goal"]["id"], "simulation_id": plan["simulation"]["id"],
            "user_id": "tester", "approved_at": datetime.now(timezone.utc).isoformat(), "scope": "host_document"}
    st, err = call(srv, "/v1/approve", {"plan_id": plan["plan_id"], "approval": appr, "scene": room_scene(blind_value=1.0)})
    assert st == 400 and "scene changed" in err["message"]
    assert call(srv, "/v1/approve", {"plan_id": "../x", "approval": appr, "scene": scene})[0] == 400


def test_manual_goal_and_hardware_refused(srv):
    scene = room_scene(blind_value=1.4486)
    st, plan = call(srv, "/v1/plan", {"scene": scene, "manual_goal": {"target_state": {
        "object_type": "blind", "object_id": BLIND_ID, "joint": "tilt", "target_value": 0.6, "intent": "reduce_direct_sun"}}})
    assert plan["state"] == "awaiting_approval" and plan["goal"]["source"] == "manual"
    appr = {"schema_version": "1.0.0", "proposal_id": plan["goal"]["id"], "simulation_id": plan["simulation"]["id"],
            "user_id": "tester", "approved_at": datetime.now(timezone.utc).isoformat(), "scope": "hardware"}
    st, err = call(srv, "/v1/approve", {"plan_id": plan["plan_id"], "approval": appr, "scene": scene})
    assert st == 400 and "hardware" in err["message"]
