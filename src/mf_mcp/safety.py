"""Guardrails: TSO command allowlist, rate limiting, error detection, audit log."""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


class Refused(Exception):
    """A request the server will not carry out."""


_SAFE_CHARS = re.compile(r"^[A-Za-z0-9 .'()*%$#@,=_\-]+$")


def check_tso_command(command: str, allowlist: list[str]) -> str:
    """Return the normalised command if it is an allow-listed read-only command."""
    cmd = command.strip()
    if not cmd:
        raise Refused("empty command")
    if len(cmd) > 200:
        raise Refused("command too long")
    if not _SAFE_CHARS.match(cmd):
        raise Refused("command contains characters that are not allowed")
    verb = cmd.split()[0].upper()
    if verb not in allowlist:
        raise Refused(f"TSO command {verb!r} is not on the read-only allowlist ({', '.join(allowlist)})")
    return cmd


_DATASET = re.compile(r"^[A-Z#$@][A-Z0-9#$@\-]{0,7}(\.[A-Z#$@][A-Z0-9#$@\-]{0,7})*$")


def check_dataset_name(name: str) -> str:
    dsn = name.strip().strip("'").upper()
    if len(dsn) > 44 or not _DATASET.match(dsn):
        raise Refused(f"{name!r} is not a valid dataset name")
    return dsn


_JOBNAME = re.compile(r"^[A-Z#$@][A-Z0-9#$@]{0,7}(\(JOB\d{5,7}\))?$")


def check_jobname(name: str) -> str:
    job = name.strip().upper()
    if not _JOBNAME.match(job):
        raise Refused(f"{name!r} is not a valid jobname (e.g. MYJOB or MYJOB(JOB01234))")
    return job


# Abend / error signatures commonly seen on TSO, batch and CICS screens.
_ERROR_PATTERNS = [
    (r"\bABEND[ =]?(S?[0-9A-F]{3}|U\d{4})\b", "abend"),
    (r"\b(S0C[1-9A-F]|S[0-9A-F]{3} ABEND|U\d{4} ABEND)\b", "abend"),
    (r"\b(ASRA|ASRB|AICA|AEY7|APCT|AEXL)\b", "CICS abend"),
    (r"\bDFHAC2\d{3}\b", "CICS abnormal termination"),
    (r"\bIEC\d{3}I\b", "data management error"),
    (r"\bICH40\d{3}I\b|\bNOT AUTHORIZED\b", "security (RACF) denial"),
    (r"\bIKJ56425I\b", "user already logged on"),
    (r"\bIKJ\d{5}[EI]? .*(NOT FOUND|INVALID|REJECTED)", "TSO error"),
]


def detect_errors(rows: list[str]) -> list[str]:
    text = "\n".join(rows)
    found: list[str] = []
    for pattern, label in _ERROR_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            found.append(f"{label}: {m.group(0).strip()}")
    return sorted(set(found))


class RateLimiter:
    def __init__(self, calls_per_minute: int):
        self._limit = max(1, calls_per_minute)
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def check(self) -> None:
        now = time.monotonic()
        with self._lock:
            while self._calls and now - self._calls[0] > 60:
                self._calls.popleft()
            if len(self._calls) >= self._limit:
                raise Refused(f"rate limit of {self._limit} calls/minute reached; try again shortly")
            self._calls.append(now)


class AuditLog:
    """Append-only JSON-lines audit trail. Secrets are redacted before writing."""

    def __init__(self, path: str, secrets: list[str]):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._secrets = [s for s in secrets if s]
        self._lock = threading.Lock()

    def redact(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "********")
        return text

    def write(self, tool: str, args: dict, outcome: str, detail: str = "") -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "args": json.loads(self.redact(json.dumps(args))),
            "outcome": outcome,
            "detail": self.redact(detail)[:500],
        }
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
