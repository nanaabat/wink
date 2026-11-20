"""wink for Blender: goal (from Hunch or typed by you) -> signal provenance -> recommended tinker skill ->
simulation status -> your approval -> outcome -> feedback. Talks to the local wink service over loopback."""
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty, StringProperty

from . import _host as H
try:  # hunch + tingle sections ship with the tingle build; tinker/wink also run without them
    from . import hunch_panel as HP
    from . import tingle_panel as TP
except ImportError:
    HP = TP = None

_PLAN = "wink_last_plan.json"
_UNDO = "wink_undo.json"


class WinkError(RuntimeError):
    pass


def _tj(name, default=None):
    t = bpy.data.texts.get(name)
    if t is None:
        return default
    try:
        return json.loads(t.as_string())
    except ValueError:
        return default


def _stj(name, v):
    t = bpy.data.texts.get(name) or bpy.data.texts.new(name)
    t.clear()
    t.write(json.dumps(v))


def _prefs(context):
    a = context.preferences.addons.get(__package__)
    return a.preferences if a else None


def _call(context, path, body=None):
    p = _prefs(context)
    port = p.port if p else 47616
    tok_file = Path(p.token_file) if p and p.token_file else Path(os.environ.get("WINK_HOME", Path.home() / ".wink")) / "token"
    try:
        tok = tok_file.read_text("utf-8").strip()
    except OSError as e:
        raise WinkError(f"wink token not found at {tok_file}; start the wink service") from e
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("message", e.reason)
        except Exception:
            msg = e.reason
        raise WinkError(str(msg)) from e
    except urllib.error.URLError as e:
        raise WinkError(f"wink service is not running: {e.reason}") from e


class WinkPrefs(bpy.types.AddonPreferences):
    bl_idname = __package__
    port: IntProperty(name="Service port", default=47616, min=1024, max=65535)
    token_file: StringProperty(name="Token file", subtype="FILE_PATH", default="")

    def draw(self, context):
        self.layout.prop(self, "port")
        self.layout.prop(self, "token_file")


class WinkState(bpy.types.PropertyGroup):
    use_hunch: BoolProperty(name="Goal from Hunch", default=True)
    time_of_day: EnumProperty(name="Time", items=[("morning", "morning", ""), ("afternoon", "afternoon", ""), ("evening", "evening", "")],
                              default="afternoon")
    rule_text: StringProperty(name="Your rule", default="make the room calmer in afternoon sun")
    manual_value: FloatProperty(name="Manual target", default=30.0, description="degrees for hinges, scene units for slides")
    sun_dir: FloatVectorProperty(name="Sun direction", size=3, default=(-0.55, -0.7, 0.45))
    status: StringProperty(default="enter a goal or ask Hunch, then Plan")
    state: StringProperty(default="")
    feedback_given: BoolProperty(default=False)


def _st(c):
    return c.scene.wink


class WINK_OT_plan(bpy.types.Operator):
    bl_idname = "wink.plan"
    bl_label = "Plan"

    def execute(self, context):
        st = _st(context)
        scene = H.snapshot(context)
        body = {"scene": scene, "sun_dir": list(st.sun_dir), "context": {"time_of_day": st.time_of_day}}
        body["context"]["object_types"] = sorted({o["type"] for o in scene.get("objects") or [] if o.get("joints")})
        if st.use_hunch and HP is not None:
            body["preferences"] = [{"id": "rule-blender", "kind": "user_rule", "when": st.time_of_day, "intent": "calmer", "text": st.rule_text}]
            try:
                body["context"]["hunch_building"] = HP.HS.compact_snapshot(HP._snapshot(context))
            except Exception:
                pass
        else:
            obj = context.active_object
            jd = H_plain(obj["tinker_joint"]) if obj is not None and obj.get("tinker_joint") else None
            if jd is None:
                self.report({"ERROR"}, "select an object with a tinker joint for a manual goal")
                return {"CANCELLED"}
            val = st.manual_value * 3.141592653589793 / 180 if jd["kind"] == "revolute" else st.manual_value
            body["manual_goal"] = {"target_state": {"object_type": obj.get("tinker_type", "generic"), "object_id": obj.get("tinker_id", obj.name),
                                                    "joint": jd["name"], "target_value": val, "intent": "reduce_direct_sun"}}
        try:
            plan = _call(context, "/v1/plan", body)
        except WinkError as e:
            st.status = f"plan failed: {e}"
            self.report({"ERROR"}, st.status)
            return {"CANCELLED"}
        plan["_scene"] = scene
        _stj(_PLAN, plan)
        st.state, st.feedback_given = plan["state"], False
        st.status = "waiting for your approval" if plan["state"] == "awaiting_approval" else (plan["error"] or plan["state"])
        return {"FINISHED"}


def H_plain(x):
    if hasattr(x, "to_dict"):
        x = x.to_dict()
    if isinstance(x, dict):
        return {k: H_plain(v) for k, v in x.items()}
    if hasattr(x, "to_list"):
        x = x.to_list()
    if isinstance(x, (list, tuple)):
        return [H_plain(v) for v in x]
    return x


class WINK_OT_approve(bpy.types.Operator):
    bl_idname = "wink.approve"
    bl_label = "Approve and apply"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        st = _st(context)
        plan = _tj(_PLAN)
        if not plan or plan["state"] != "awaiting_approval":
            self.report({"ERROR"}, "no plan waiting for approval")
            return {"CANCELLED"}
        approval = {"schema_version": "1.0.0", "proposal_id": plan["goal"]["id"], "simulation_id": plan["simulation"]["id"],
                    "user_id": "blender-user", "approved_at": datetime.now(timezone.utc).isoformat(), "scope": "host_document"}
        try:
            out = _call(context, "/v1/approve", {"plan_id": plan["plan_id"], "approval": approval, "scene": H.snapshot(context)})
        except WinkError as e:
            st.status = f"approval refused: {e}"
            self.report({"ERROR"}, st.status)
            return {"CANCELLED"}
        undo = []
        err = None
        try:
            for mv in out["moves"]:
                obj = H.object_by_id(context, mv["object_id"])
                if obj is None:
                    raise WinkError(f"object {mv['object_id']} not found")
                undo.append({"name": obj.name, "matrix": [list(r) for r in obj.matrix_world], "joint": H_plain(obj["tinker_joint"])})
                snap = H.snapshot(context)
                from ._vendor.tw_scene import find_joint, find_object, pose_for_value
                so = find_object(snap, mv["object_id"])
                j = find_joint(so, mv["joint"])
                H.set_object_pose(obj, pose_for_value(so, j, mv["value"]))
                jd = H_plain(obj["tinker_joint"])
                jd["value"] = float(mv["value"])
                obj["tinker_joint"] = jd
            context.view_layer.update()
            try:
                bpy.ops.ed.undo_push(message="wink apply")
            except RuntimeError:
                pass
        except Exception as e:  # roll back whatever was applied
            err = str(e)
            for u in reversed(undo):
                from mathutils import Matrix
                o = context.scene.objects[u["name"]]
                o.matrix_world = Matrix(u["matrix"])
                o["tinker_joint"] = u["joint"]
            undo = []
        _stj(_UNDO, undo)
        rc = _call(context, "/v1/confirm", {"plan_id": plan["plan_id"], "scene": H.snapshot(context),
                                            "host_undo_ref": "blender:wink apply" if undo else None, "error": err})
        if rc["result"] != "applied":
            bpy.ops.wink.undo()
        plan["receipt"], plan["state"] = rc, "recorded" if rc["result"] == "applied" else rc["result"]
        _stj(_PLAN, plan)
        st.state = plan["state"]
        st.status = f"outcome: {rc['result']} ({rc['audit_id']})"
        return {"FINISHED"}


class WINK_OT_reject(bpy.types.Operator):
    bl_idname = "wink.reject"
    bl_label = "Reject"

    def execute(self, context):
        plan = _tj(_PLAN)
        if not plan:
            return {"CANCELLED"}
        try:
            out = _call(context, "/v1/reject", {"plan_id": plan["plan_id"], "user_id": "blender-user"})
        except WinkError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        plan["state"] = out["state"]
        _stj(_PLAN, plan)
        _st(context).state, _st(context).status = out["state"], "rejected: nothing changed"
        return {"FINISHED"}


class WINK_OT_feedback(bpy.types.Operator):
    bl_idname = "wink.feedback"
    bl_label = "Feedback"
    rating: IntProperty(default=1, min=-1, max=1)

    def execute(self, context):
        plan = _tj(_PLAN)
        if not plan:
            return {"CANCELLED"}
        _call(context, "/v1/feedback", {"plan_id": plan["plan_id"], "rating": self.rating})
        _st(context).feedback_given = True
        _st(context).status = "thanks, feedback recorded"
        return {"FINISHED"}


class WINK_OT_undo(bpy.types.Operator):
    bl_idname = "wink.undo"
    bl_label = "Undo"

    def execute(self, context):
        from mathutils import Matrix
        for u in reversed(_tj(_UNDO, []) or []):
            o = context.scene.objects.get(u["name"])
            if o is not None:
                o.matrix_world = Matrix(u["matrix"])
                o["tinker_joint"] = u["joint"]
        _stj(_UNDO, [])
        _st(context).status = "undone"
        return {"FINISHED"}


class WINK_PT_panel(bpy.types.Panel):
    bl_label = "wink"
    bl_idname = "WINK_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "wink"

    def draw(self, context):
        st = _st(context)
        L = self.layout
        L.label(text="the intelligence that brings them together")
        L.box().label(text=st.status, icon="INFO")
        col = L.column(align=True)
        col.prop(st, "use_hunch")
        if st.use_hunch:
            col.prop(st, "time_of_day")
            col.prop(st, "rule_text")
        else:
            col.prop(st, "manual_value")
        col.prop(st, "sun_dir")
        col.operator("wink.plan", icon="VIEWZOOM")
        plan = _tj(_PLAN)
        if plan:
            box = L.box()
            for line in plan.get("explanation", [])[:12]:
                for i in range(0, len(line), 60):
                    box.label(text=line[i:i + 60])
            if plan.get("state") == "awaiting_approval":
                row = L.row(align=True)
                row.operator("wink.approve", icon="CHECKMARK")
                row.operator("wink.reject", icon="X")
            if plan.get("receipt"):
                L.label(text=f"outcome: {plan['receipt']['result']}")
                row = L.row(align=True)
                row.enabled = not st.feedback_given
                row.operator("wink.feedback", text="helpful", icon="CHECKBOX_HLT").rating = 1
                row.operator("wink.feedback", text="not helpful", icon="CHECKBOX_DEHLT").rating = -1
                L.operator("wink.undo", icon="LOOP_BACK")
        if HP is not None:
            HP.draw_hunch_section(L, context)


CLASSES = (WinkPrefs, WinkState, WINK_OT_plan, WINK_OT_approve, WINK_OT_reject, WINK_OT_feedback, WINK_OT_undo)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.wink = PointerProperty(type=WinkState)
    if HP is not None:
        HP.register()
    if TP is not None:
        TP.register()


def unregister():
    if TP is not None:
        TP.unregister()
    if HP is not None:
        HP.unregister()
    del bpy.types.Scene.wink
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
