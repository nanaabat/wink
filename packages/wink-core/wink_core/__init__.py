"""wink-core: the coordinator. observe -> propose goal -> choose skill -> constraint check -> simulate -> explain ->
preview -> explicit approval -> apply -> verify -> record. Holds no Hunch or Tinker models."""
from .audit import AuditLog, AuditIntegrityError
from .policy import Policy
from .orchestrator import Wink, Plan, WinkError, PolicyViolation

__version__ = "0.1.0"
__all__ = ["AuditLog", "AuditIntegrityError", "Policy", "Wink", "Plan", "WinkError", "PolicyViolation"]
