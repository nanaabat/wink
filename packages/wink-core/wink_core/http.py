"""wink local service for hosts that cannot import wink (e.g. Blender's add-on). Same loopback security as tinker.
The host pushes its scene with each call and applies approved moves itself, then confirms; wink verifies."""
from __future__ import annotations

import os
import re
from pathlib import Path

from tw_connector import PushedSceneConnector
from tw_connector.ipc import SecureServer, load_or_create_token
from tw_contracts import ContractError, validate

from .audit import AuditLog
from .orchestrator import PolicyViolation, Wink, WinkError
from .policy import Policy

_ID = re.compile(r"^plan-[0-9a-f]{12}$")


def _plan_json(p) -> dict:
    return {"plan_id": p.id, "state": p.state, "error": p.error, "goal": p.goal, "observations": p.observations,
            "skill": {k: p.skill[k] for k in ("id", "name", "version", "model_ref", "evaluation")} if p.skill else None,
            "target": p.target, "simulation": p.simulation, "preview": (p.preview or {}).get("frames", []),
            "explanation": p.explanation, "suggestions": p.suggestions, "receipt": p.receipt}


class WinkHTTPServer(SecureServer):
    client_errors = (ContractError, ValueError, KeyError, WinkError)

    def __init__(self, token: str, audit_path: Path, *, hunch=None, tinker_registry: Path | None = None, port: int = 0,
                 policy: Policy | None = None):
        super().__init__(token, port)
        self.connector = PushedSceneConnector()
        tinker = None
        if tinker_registry is not None:
            try:  # Tinker is optional: without it wink only suggests
                from tinker_learning import ModelRegistry
                from tinker_service import TinkerProvider
                tinker = TinkerProvider(ModelRegistry(tinker_registry), self.connector)
            except ImportError:
                tinker = None
        self.wink = Wink(connector=self.connector, audit=AuditLog(audit_path), hunch=hunch, tinker=tinker, policy=policy)

    def get_routes(self, path):
        if path == "/v1/health":
            return {"ok": True, "service": "wink", "version": "0.1.0", "hunch": getattr(self.wink.hunch, "name", None),
                    "hunch_mock": getattr(self.wink.hunch, "is_mock", None), "tinker": self.wink.tinker is not None}
        if path == "/v1/audit/verify":
            return {"entries": len(self.wink.audit.verify()), "ok": True}
        return None

    def _plan(self, body):
        pid = body.get("plan_id", "")
        if not isinstance(pid, str) or not _ID.match(pid):
            raise ValueError("invalid plan id")
        return pid

    def routes(self, path, body):
        w = self.wink
        try:
            if path == "/v1/plan":
                self.connector.push(validate("SceneSnapshot", body["scene"]))
                w.sun_dir = body.get("sun_dir")
                ctx = body.get("context") or {}
                if w.sun_dir is not None and "sunlit_fraction" not in ctx:
                    try:
                        from tw_sim import sunlit_fraction
                        frac = sunlit_fraction(body["scene"], w.sun_dir)
                        if frac is not None:
                            ctx["sunlit_fraction"] = frac
                    except Exception:
                        pass
                p = w.plan(context=ctx, preferences=body.get("preferences") or [], manual_goal=body.get("manual_goal"))
                return _plan_json(p)
            if path == "/v1/approve":
                scene = validate("SceneSnapshot", body["scene"])
                return w.approve(self._plan(body), body["approval"], scene)
            if path == "/v1/confirm":
                scene = validate("SceneSnapshot", body["scene"])
                return w.confirm_applied(self._plan(body), scene, body.get("host_undo_ref"), body.get("error"))
            if path == "/v1/reject":
                return _plan_json(w.reject(self._plan(body), str(body.get("user_id", "user"))[:64], str(body.get("reason", ""))))
            if path == "/v1/feedback":
                w.feedback(self._plan(body), int(body.get("rating", 0)), str(body.get("note", "")))
                return {"ok": True}
        except PolicyViolation as e:
            raise WinkError(str(e)) from e
        return None


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="wink local service (loopback only)")
    home = Path(os.environ.get("WINK_HOME", Path.home() / ".wink"))
    ap.add_argument("--token-file", default=str(home / "token"))
    ap.add_argument("--audit", default=str(home / "audit.jsonl"))
    ap.add_argument("--tinker-registry", default=str(Path(os.environ.get("TINKER_HOME", Path.home() / ".tinker")) / "registry"))
    ap.add_argument("--hunch", choices=["mock", "local", "scene", "none"], default="mock")
    ap.add_argument("--hunch-root")
    ap.add_argument("--hunch-model")
    ap.add_argument("--port", type=int, default=47616)
    a = ap.parse_args(argv)
    hunch = None
    if a.hunch == "mock":
        from hunch_adapter import MockHunchProvider
        hunch = MockHunchProvider()
    elif a.hunch == "local":
        from hunch_adapter.local import LocalHunchProvider
        hunch = LocalHunchProvider(a.hunch_root, a.hunch_model)
    elif a.hunch == "scene":
        from hunch_adapter import SceneHunchProvider
        hunch = SceneHunchProvider(hunch_root=a.hunch_root)
    srv = WinkHTTPServer(load_or_create_token(Path(a.token_file)), Path(a.audit), hunch=hunch,
                         tinker_registry=Path(a.tinker_registry), port=a.port)
    print(f"wink-service on http://127.0.0.1:{srv.server_address[1]} (token in {a.token_file})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
