# wink

the intelligence that brings them together. connect. decide. act.

wink takes a goal, from hunch or from you, checks which tinker skills can reach it, runs them in the sandbox, and asks before it changes anything. every plan has two steps: you approve, then it acts. every step is written to an audit log you can verify.

## what is here

packages/wink-core  the coordinator: policy, two step approval, audit log, and a local http service
packages/hunch-interface  the provider protocol wink uses to ask hunch for a goal, a guard that blocks claims about reading people's emotions, and a deterministic mock
blender/wink_blender  the blender extension: plan, approve, apply, undo

the hunch engine is not in this repo. wink runs without it: with the mock, or with goals you type.

## try it

wink builds on the shared packages in the tinker repo. clone both side by side.

```
python -m venv .venv
.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install onnx onnxruntime onnxscript mujoco jsonschema pytest
for p in ../tinker/packages/* packages/*; do .venv/bin/pip install -e "$p"; done
.venv/bin/python -m pytest
.venv/bin/wink-service --hunch mock
python build_blender.py --tinker ../tinker
```

## status

experimental and early. expect rough edges and changes, and check every result yourself. no trained wink model ships with this repo. our internal wink model is trained on synthetic bootstrap data and is not included.

 plan, approve, apply and undo run end to end in blender against the mock and against a real hunch on a test scene. wink only acts through skills tinker has learned; without one it suggests and does nothing.

## family

tinker learns the moves. wink decides when to use them. tingle puts hunch, tinker and wink in one panel.

## licence

mit, copyright aprewaturkson inc. see LICENSE.
