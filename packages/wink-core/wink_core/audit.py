"""Append-only, hash-chained JSONL audit log. Any edit, deletion or reordering breaks verification."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


class AuditIntegrityError(RuntimeError):
    pass


def _h(prev: str, entry: dict) -> str:
    body = json.dumps({k: v for k, v in entry.items() if k != "hash"}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev + body).encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last, self._seq = GENESIS, 0
        if self.path.exists():
            entries = self.verify()
            if entries:
                self._last, self._seq = entries[-1]["hash"], entries[-1]["seq"]

    def append(self, kind: str, data: dict) -> str:
        with self._lock:
            self._seq += 1
            e = {"seq": self._seq, "ts": datetime.now(timezone.utc).isoformat(), "kind": kind, "data": data, "prev": self._last}
            e["hash"] = _h(self._last, e)
            line = json.dumps(e, sort_keys=True) + "\n"
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            self._last = e["hash"]
            return f"audit-{self._seq:06d}-{e['hash'][:12]}"

    def verify(self) -> list[dict]:
        prev, out = GENESIS, []
        if not self.path.exists():
            return out
        with open(self.path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError as ex:
                    raise AuditIntegrityError(f"line {i} is not JSON") from ex
                if e.get("prev") != prev or e.get("seq") != len(out) + 1 or _h(prev, e) != e.get("hash"):
                    raise AuditIntegrityError(f"audit chain broken at line {i}")
                prev = e["hash"]
                out.append(e)
        return out
