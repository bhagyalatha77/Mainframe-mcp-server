"""TN3270 terminal wrapper around IBM's `tnz` library (Apache-2.0, pure Python).

`tnz` drives its own asyncio loop and is not thread-safe, so every call is
funnelled onto one dedicated worker thread. Callers use a plain synchronous API.
"""
from __future__ import annotations

import asyncio
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

AID_KEYS = {"ENTER", "CLEAR", "PA1", "PA2", "PA3"} | {f"PF{i}" for i in range(1, 25)}


class TerminalError(RuntimeError):
    pass


class Terminal(Protocol):
    """What the service layer needs from a 3270 terminal (lets tests use a fake)."""

    def connect(self) -> None: ...
    def is_connected(self) -> bool: ...
    def rows(self) -> list[str]: ...
    def type_text(self, text: str) -> None: ...
    def press(self, key: str) -> None: ...
    def settle(self, timeout: float) -> None: ...
    def close(self) -> None: ...


class TnzTerminal:
    def __init__(self, host: str, port: int, tls: bool, verify_cert: bool, connect_timeout: float):
        self._host, self._port = host, port
        self._tls, self._verify = tls, verify_cert
        self._connect_timeout = connect_timeout
        self._tn = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tn3270",
                                        initializer=lambda: asyncio.set_event_loop(asyncio.new_event_loop()))

    # -- plumbing ---------------------------------------------------------
    def _run(self, fn, *args):
        return self._pool.submit(fn, *args).result()

    def _require(self):
        if self._tn is None or self._tn.seslost:
            raise TerminalError("not connected to the mainframe; call connect first")
        return self._tn

    # -- public API (each hops onto the tn3270 thread) ----------------------
    def connect(self) -> None:
        self._run(self._connect)

    def _connect(self) -> None:
        from tnz import tnz  # imported lazily so tests don't need a host

        if not self._host:
            raise TerminalError("MF_HOST is not configured")
        self._tn = tnz.connect(self._host, self._port, secure=self._tls,
                               verifycert=self._verify if self._tls else None)
        deadline = time.monotonic() + self._connect_timeout
        while time.monotonic() < deadline and not self._tn.updated and not self._tn.seslost:
            self._tn.wait(0.5)
        if self._tn.seslost:
            raise TerminalError(f"connection to {self._host}:{self._port} was lost")
        if not self._tn.updated:
            raise TerminalError(f"no screen received from {self._host}:{self._port} within timeout")
        self._settle(2.0)

    def is_connected(self) -> bool:
        return self._tn is not None and not self._tn.seslost

    def rows(self) -> list[str]:
        return self._run(self._rows)

    def _rows(self) -> list[str]:
        tn = self._require()
        text = tn.scrstr(0, 0, rstrip=False)
        cols = tn.maxcol
        return [text[i:i + cols].rstrip() for i in range(0, tn.maxrow * cols, cols)]

    def type_text(self, text: str) -> None:
        self._run(lambda: self._require().key_data(text))

    def press(self, key: str) -> None:
        key = key.upper()
        if key not in AID_KEYS:
            raise TerminalError(f"unsupported key {key!r}")
        self._run(lambda: getattr(self._require(), key.lower())())

    def settle(self, timeout: float) -> None:
        self._run(self._settle, timeout)

    def _settle(self, timeout: float, quiet: float = 0.6) -> None:
        """Wait until the host has finished painting (no updates for `quiet` s)."""
        tn = self._require()
        deadline = time.monotonic() + timeout
        last_change = time.monotonic()
        while time.monotonic() < deadline:
            tn.updated = False
            tn.wait(0.2)
            if tn.seslost:
                raise TerminalError("connection lost while waiting for the host")
            now = time.monotonic()
            if tn.updated:
                last_change = now
            elif not tn.pwait and now - last_change >= quiet:
                return

    def close(self) -> None:
        def _close():
            if self._tn is not None:
                try:
                    self._tn.shutdown()
                except Exception:  # best effort on an already-dead socket
                    pass
                self._tn = None
        self._run(_close)


def find(rows: list[str], pattern: str) -> re.Match | None:
    return re.search(pattern, "\n".join(rows), re.IGNORECASE | re.MULTILINE)
