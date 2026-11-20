"""Runs INSIDE real Blender (background). wink extension + wink service (mock Hunch, real tinker skill):
plan -> provenance/explanation -> approve -> Blender applies -> wink verifies -> receipt -> feedback -> undo;
plus reject, stale-scene refusal and a manual goal.
usage: blender -b --factory-startup --python blender_smoke.py -- ZIP FIXTURE PORT TOKEN OUT"""
import json
import sys
import traceback

import bpy

ZIP, FIXTURE, PORT, TOKEN, OUT = sys.argv[sys.argv.index("--") + 1:]
res = {"blender": bpy.app.version_string, "checks": {}, "errors": []}


def check(n, c, d=""):
    res["checks"][n] = {"ok": bool(c), "detail": str(d)[:600]}


try:
    bpy.ops.extensions.package_install_files(filepath=ZIP, repo="user_default", enable_on_install=True)
    import importlib
    name = next(m for m in bpy.context.preferences.addons.keys() if m.endswith("wink_blender"))
    p = bpy.context.preferences.addons[name].preferences
    p.port, p.token_file = int(PORT), TOKEN
    wb = importlib.import_module(name)
    check("extension_installed_and_panel_registered", hasattr(bpy.types, "TINGLE_PT_panel") and hasattr(bpy.types, "WINK_OT_plan"), name)
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    ctx = bpy.context
    made = wb.H.build_from_snapshot(ctx, json.load(open(FIXTURE)))
    blind = next(o for o in made.values() if o.get("tinker_type") == "blind")
    st = ctx.scene.wink
    val = lambda: blind["tinker_joint"]["value"]
    v0 = val()

    bpy.ops.wink.plan()
    plan = json.loads(bpy.data.texts["wink_last_plan.json"].as_string())
    text = "\n".join(plan["explanation"])
    res["explanation"] = plan["explanation"]
    check("plan_awaiting_approval", plan["state"] == "awaiting_approval", st.status)
    check("provenance_shown", "your rule" in text and "[mock]" in text and "signal:" in text, text[:300])
    check("skill_and_simulation_shown", "skill: blind-tilt" in text and "simulation passed" in text)
    check("nothing_moved_before_approval", abs(val() - v0) < 1e-9)

    bpy.ops.wink.approve()
    plan = json.loads(bpy.data.texts["wink_last_plan.json"].as_string())
    target = plan["target"]["to"]
    check("applied_and_verified", plan["receipt"]["result"] == "applied" and abs(val() - target) < 1e-6,
          f"{plan['receipt']['result']} {val():.4f} vs {target:.4f}")
    bpy.ops.wink.feedback(rating=1)
    check("feedback", st.feedback_given)
    bpy.ops.wink.undo()
    check("undo", abs(val() - v0) < 1e-6, val())

    bpy.ops.wink.plan()
    bpy.ops.wink.reject()
    check("reject_leaves_scene", st.state == "rejected" and abs(val() - v0) < 1e-6, st.state)

    bpy.ops.wink.plan()
    blind.location.z += 0.01  # someone edits the scene after planning
    ctx.view_layer.update()
    try:
        r = bpy.ops.wink.approve()
        refused = r == {"CANCELLED"}
    except RuntimeError as e:
        refused = "scene changed" in str(e)
    check("stale_scene_refused", refused, st.status)
    blind.location.z -= 0.01
    ctx.view_layer.update()

    st.use_hunch = False
    st.manual_value = 25.0
    ctx.view_layer.objects.active = blind
    bpy.ops.wink.plan()
    plan = json.loads(bpy.data.texts["wink_last_plan.json"].as_string())
    check("manual_goal", plan["state"] == "awaiting_approval" and plan["goal"]["source"] == "manual", plan["state"])
except Exception:
    res["errors"].append(traceback.format_exc())
json.dump(res, open(OUT, "w"), indent=2)
