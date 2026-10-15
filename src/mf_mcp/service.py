"""Mainframe operations built on a Terminal: logon, TSO commands, screen access.

One TSO session is shared by every caller, so all operations are serialised
with a lock. The session runs under one service userid configured by the
operator; the agent can never supply credentials.
"""
from __future__ import annotations

import re
import threading

from .config import Settings
from .safety import Refused, check_dataset_name, check_jobname, check_tso_command, detect_errors
from .terminal import AID_KEYS, Terminal, TerminalError

_READY = re.compile(r"^\s*READY\s*$")
_MORE = re.compile(r"\*\*\*\s*$")
_PASSWORD_PROMPT = re.compile(r"ENTER (CURRENT )?PASSWORD|PASSWORD\s*===>|^\s*PASSWORD\b", re.I | re.M)
_USERID_PROMPT = re.compile(r"ENTER USERID|USERID\s*===>\s*$", re.I | re.M)
_ISPF = re.compile(r"ISPF PRIMARY OPTION MENU|^\s*OPTION\s*===>", re.I | re.M)
_LOGON_FAIL = re.compile(r"IKJ56425I|ALREADY LOGGED ON|PASSWORD NOT AUTHORIZED|NOT AUTHORIZED|"
                         r"USERID .* NOT DEFINED|REVOKED|IKJ56(4|7)\d\dI .*(INVALID|NOT)", re.I)
MAX_PAGES = 40


def format_screen(rows: list[str]) -> str:
    """Row-numbered screen text, easy for an AI to reference by line."""
    return "\n".join(f"{i + 1:2}| {row}" for i, row in enumerate(rows))


def _last_line(rows: list[str]) -> str:
    for row in reversed(rows):
        if row.strip():
            return row
    return ""


class MainframeService:
    def __init__(self, settings: Settings, terminal: Terminal):
        self.s = settings
        self.t = terminal
        self.lock = threading.RLock()
        self.logged_on = False

    # -- session ------------------------------------------------------------
    def status(self) -> str:
        return (f"host={self.s.host}:{self.s.port} tls={self.s.tls} userid={self.s.userid or '(unset)'} "
                f"connected={self.t.is_connected()} logged_on={self.logged_on and self.t.is_connected()}")

    def _at_ready(self, rows: list[str]) -> bool:
        return bool(_READY.match(_last_line(rows)))

    def logon(self) -> str:
        if not (self.s.userid and self.s.password):
            raise Refused("MF_USERID / MF_PASSWORD are not configured on the server")
        if not self.t.is_connected():
            self.logged_on = False
            self.t.connect()
        sent_applid = False
        sent_password = False
        for _ in range(20):
            rows = self.t.rows()
            text = "\n".join(rows)
            if self._at_ready(rows):
                self.logged_on = True
                return "Logged on; TSO READY.\n" + format_screen(rows)
            if _LOGON_FAIL.search(text):
                raise TerminalError("logon rejected by host:\n" + format_screen(rows))
            if _ISPF.search(text):
                # Auto-started ISPF: leave it so line-mode TSO commands work.
                self.t.type_text("=X")
                self.t.press("ENTER")
            elif _PASSWORD_PROMPT.search(text) and not sent_password:
                self.t.type_text(self.s.password)
                self.t.press("ENTER")
                sent_password = True
            elif _USERID_PROMPT.search(text):
                self.t.type_text(self.s.userid)
                self.t.press("ENTER")
            elif _MORE.search(_last_line(rows)):
                self.t.press("ENTER")
            elif self.s.applid and not sent_applid:
                self.t.type_text(self.s.applid.format(userid=self.s.userid))
                self.t.press("ENTER")
                sent_applid = True
            else:
                self.t.press("ENTER")
            self.t.settle(self.s.command_timeout)
        raise TerminalError("did not reach TSO READY during logon. Last screen:\n" + format_screen(self.t.rows()))

    def ensure_ready(self) -> None:
        if not (self.t.is_connected() and self.logged_on):
            self.logon()
            return
        rows = self.t.rows()
        if self._at_ready(rows):
            return
        if _MORE.search(_last_line(rows)):
            self.t.press("ENTER")
            self.t.settle(self.s.command_timeout)
            if self._at_ready(self.t.rows()):
                return
        # Try to get back to READY without guessing too hard.
        self.t.press("PA1")
        self.t.settle(self.s.command_timeout)
        if not self._at_ready(self.t.rows()):
            self.t.press("ENTER")
            self.t.settle(self.s.command_timeout)
        if not self._at_ready(self.t.rows()):
            raise TerminalError("session is not at TSO READY; inspect with get_screen. Current screen:\n"
                                + format_screen(self.t.rows()))

    def logoff(self) -> str:
        if self.t.is_connected():
            try:
                self.ensure_ready()
                self.t.type_text("LOGOFF")
                self.t.press("ENTER")
                self.t.settle(5)
            except (TerminalError, Refused):
                pass
            self.t.close()
        self.logged_on = False
        return "Logged off and disconnected."

    # -- TSO line-mode commands ---------------------------------------------
    def run_tso(self, command: str) -> str:
        cmd = check_tso_command(command, self.s.tso_allowlist)
        self.ensure_ready()
        self.t.press("CLEAR")
        self.t.settle(5)
        self.t.type_text(cmd)
        self.t.press("ENTER")
        self.t.settle(self.s.command_timeout)

        output: list[str] = []
        first = True
        for _ in range(MAX_PAGES):
            rows = self._wait_for_page()
            page = list(rows)
            if first:
                # Drop everything up to and including the echoed command.
                for idx, row in enumerate(page):
                    if row.strip().upper() == cmd.upper():
                        page = page[idx + 1:]
                        break
                first = False
            done = self._at_ready(rows)
            more = _MORE.search(_last_line(rows))
            for row in page:
                if _READY.match(row) and done:
                    continue
                if _MORE.search(row):
                    row = _MORE.sub("", row)
                output.append(row)
            if done:
                break
            if more:
                self.t.press("ENTER")
            self.t.settle(self.s.command_timeout)
        else:
            self.t.press("PA1")
            self.t.settle(5)
            output.append(f"... output truncated after {MAX_PAGES} screens")

        while output and not output[-1].strip():
            output.pop()
        while output and not output[0].strip():
            output.pop(0)
        body = "\n".join(output) if output else "(no output)"
        errors = detect_errors(output)
        if errors:
            body += "\n\nDETECTED: " + "; ".join(errors)
        return f"$ {cmd}\n{body}"

    def _wait_for_page(self) -> list[str]:
        """Return the screen once it ends in READY or the '***' more-data marker."""
        for _ in range(3):
            rows = self.t.rows()
            if self._at_ready(rows) or _MORE.search(_last_line(rows)):
                return rows
            self.t.settle(self.s.command_timeout)
        raise TerminalError("command did not finish within timeout. Screen:\n" + format_screen(self.t.rows()))

    def list_datasets(self, prefix: str) -> str:
        return self.run_tso(f"LISTCAT LEVEL({check_dataset_name(prefix)})")

    def list_members(self, dataset: str) -> str:
        return self.run_tso(f"LISTDS '{check_dataset_name(dataset)}' MEMBERS")

    def dataset_info(self, dataset: str) -> str:
        return self.run_tso(f"LISTDS '{check_dataset_name(dataset)}'")

    def job_status(self, jobname: str) -> str:
        return self.run_tso(f"STATUS {check_jobname(jobname)}")

    # -- raw screen ---------------------------------------------------------
    def get_screen(self) -> str:
        if not self.t.is_connected():
            raise TerminalError("not connected; call mainframe_logon first")
        return format_screen(self.t.rows())

    def press_key(self, key: str) -> str:
        key = key.strip().upper()
        if key not in AID_KEYS:
            raise Refused(f"key must be ENTER, CLEAR, PF1-PF24 or PA1-PA3, not {key!r}")
        if not self.t.is_connected():
            raise TerminalError("not connected; call mainframe_logon first")
        self.t.press(key)
        self.t.settle(self.s.command_timeout)
        return format_screen(self.t.rows())

    def type_text(self, text: str, press_enter: bool) -> str:
        if not self.s.allow_free_typing:
            raise Refused("free typing is disabled (set MF_ALLOW_FREE_TYPING=true to enable). "
                          "Use run_tso_command or the dataset/job tools instead.")
        if not self.t.is_connected():
            raise TerminalError("not connected; call mainframe_logon first")
        self.t.type_text(text)
        if press_enter:
            self.t.press("ENTER")
        self.t.settle(self.s.command_timeout)
        return format_screen(self.t.rows())

    def check_for_errors(self) -> str:
        if not self.t.is_connected():
            raise TerminalError("not connected; call mainframe_logon first")
        errors = detect_errors(self.t.rows())
        return "No errors detected on the current screen." if not errors else "DETECTED: " + "; ".join(errors)
