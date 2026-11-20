from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Policy:
    max_uncertainty: float = 0.5          # proposals more uncertain than this need a manual goal instead
    min_skill_confidence: float = 0.5
    allowed_scopes: frozenset = frozenset({"sandbox_preview", "host_document"})
    hardware_enabled: bool = False        # off by default; see specs/threat-model.md
    hardware_safety_controller: object | None = None  # independent controller with interlocks + E-stop (not provided)
    approval_ttl_s: float = 600.0
    max_sunlit_fraction: float = 0.12

    def scope_allowed(self, scope: str) -> tuple[bool, str]:
        if scope == "hardware":
            if not self.hardware_enabled:
                return False, "hardware actions are disabled"
            if self.hardware_safety_controller is None:
                return False, "hardware requires an independent safety controller, interlocks, E-stop and certified review"
        if scope not in self.allowed_scopes and scope != "hardware":
            return False, f"scope {scope} not allowed"
        return True, ""
