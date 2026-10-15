"""A scripted stand-in for a z/OS TSO host, implementing the Terminal protocol."""
from __future__ import annotations

ROWS = 24


class FakeTerminal:
    def __init__(self, password: str = "SECRET1", already_logged_on: bool = False, ispf_on_logon: bool = False):
        self.password = password
        self.already_logged_on = already_logged_on
        self.ispf_on_logon = ispf_on_logon
        self.connected = False
        self.state = "down"
        self.buffer = ""
        self.lines: list[str] = []
        self.pending: list[str] = []  # line-mode output not yet shown
        self.typed: list[str] = []
        self.keys: list[str] = []

    # Terminal protocol ------------------------------------------------------
    def connect(self) -> None:
        self.connected = True
        self.state = "vtam"
        self.lines = ["   WELCOME TO THE TEST SYSTEM", "", "   ENTER APPLICATION NAME:"]

    def is_connected(self) -> bool:
        return self.connected

    def rows(self) -> list[str]:
        rows = (self.lines + [""] * ROWS)[:ROWS]
        return [r[:80] for r in rows]

    def type_text(self, text: str) -> None:
        self.typed.append(text)
        self.buffer += text

    def settle(self, timeout: float) -> None:
        pass

    def close(self) -> None:
        self.connected = False
        self.state = "down"

    def press(self, key: str) -> None:
        self.keys.append(key)
        text, self.buffer = self.buffer, ""
        if key == "CLEAR":
            self.lines = []
            return
        if key == "PA1":
            self.pending = []
            if self.state == "ready":
                self._show(["READY"])
            return
        if key != "ENTER":
            return
        getattr(self, f"_on_{self.state}")(text)

    # host behaviour ---------------------------------------------------------
    def _show(self, new_lines: list[str]) -> None:
        """Line-mode output: fill the screen, pause with *** when it is full."""
        self.pending.extend(new_lines)
        self._flush()

    def _flush(self) -> None:
        room = ROWS - len(self.lines)
        if len(self.pending) <= room:
            self.lines += self.pending
            self.pending = []
        else:
            take = room - 1
            self.lines += self.pending[:take] + [" ***"]
            self.pending = self.pending[take:]

    def _on_vtam(self, text: str) -> None:
        if text.strip().upper() == "TSO":
            self.state = "userid"
            self.lines = ["IKJ56700A ENTER USERID -"]

    def _on_userid(self, text: str) -> None:
        self.state = "password"
        self.lines = ["------------------------------- TSO/E LOGON -----------------------------------",
                      "", f"   Userid    ===> {text.strip()}", "", "   Password  ===>"]

    def _on_password(self, text: str) -> None:
        if self.already_logged_on:
            self.lines = ["IKJ56425I LOGON rejected, UserId TESTER already logged on"]
            self.state = "dead"
            return
        if text != self.password:
            self.lines = ["IKJ56421I PASSWORD NOT AUTHORIZED FOR USERID TESTER"]
            self.state = "dead"
            return
        self.lines = []
        if self.ispf_on_logon:
            self.state = "ispf"
            self.lines = ["  ISPF Primary Option Menu", "Option ===>"]
            return
        self.state = "ready"
        self._show(["ICH70001I TESTER LAST ACCESS AT 10:00:00", "READY"])

    def _on_dead(self, text: str) -> None:
        pass

    def _on_ispf(self, text: str) -> None:
        if text.strip().upper() == "=X":
            self.state = "ready"
            self.lines = []
            self._show(["READY"])

    def _on_ready(self, text: str) -> None:
        if self.pending:  # ENTER after *** shows the next page
            self.lines = []
            self._flush()
            return
        cmd = text.strip()
        if not cmd:
            return
        self._show([cmd])
        verb = cmd.split()[0].upper()
        if verb == "LISTCAT":
            out = []
            for i in range(30):
                out += [f"NONVSAM ------- TESTER.DATA.SET{i:02}", "     IN-CAT --- CATALOG.USER"]
            self._show(out + ["READY"])
        elif verb == "LISTDS":
            self._show(["TESTER.SOURCE.COBOL", "--RECFM-LRECL-BLKSIZE-DSORG", "  FB    80    27920   PO",
                        "--VOLUMES--", "  USR001", "--MEMBERS--", "  PAY001", "  PAY002", "READY"])
        elif verb == "STATUS":
            self._show(["IKJ56211I JOB TESTERA(JOB01234) ON OUTPUT QUEUE", "READY"])
        elif verb == "LOGOFF":
            self.lines = ["TESTER LOGGED OFF TSO AT 10:05:00"]
            self.state = "dead"
        else:
            self._show([f"IKJ56500I COMMAND {verb} NOT FOUND", "READY"])
