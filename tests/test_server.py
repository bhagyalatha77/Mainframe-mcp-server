import json

import anyio
import pytest
from starlette.testclient import TestClient

from fake_host import FakeTerminal
from mf_mcp.config import Settings
from mf_mcp.safety import Refused, check_dataset_name, check_jobname, check_tso_command, detect_errors
from mf_mcp.server import build_http_app, build_server
from mf_mcp.service import MainframeService


@pytest.fixture
def settings(tmp_path):
    return Settings(host="fake", userid="TESTER", password="SECRET1", api_key="k3y",
                    audit_log_path=str(tmp_path / "audit.jsonl"))


def make(settings, **kw):
    term = FakeTerminal(**kw)
    return MainframeService(settings, term), term


# -- safety ------------------------------------------------------------------
@pytest.mark.parametrize("cmd", ["DELETE 'A.B'", "SUBMIT 'A.B(C)'", "ALLOC DA(X)", "OSHELL rm -rf /",
                                 "LISTCAT; DELETE X", "RENAME A B"])
def test_disallowed_commands_refused(cmd):
    with pytest.raises(Refused):
        check_tso_command(cmd, ["LISTCAT", "LISTDS", "STATUS"])


def test_allowed_command_passes():
    assert check_tso_command(" listds 'A.B' members ", ["LISTDS"]) == "listds 'A.B' members"


@pytest.mark.parametrize("bad", ["A..B", "1ABC.DEF", "A.B') DELETE('X", "TOOLONGQUAL.X"])
def test_bad_dataset_names(bad):
    with pytest.raises(Refused):
        check_dataset_name(bad)


def test_jobname_validation():
    assert check_jobname("payjob1(JOB01234)") == "PAYJOB1(JOB01234)"
    with pytest.raises(Refused):
        check_jobname("PAY JOB; LOGOFF")


def test_detect_errors():
    found = detect_errors(["IEF450I PAYJOB STEP1 - ABEND=S0C7 U0000", "DFHAC2206 transaction failed"])
    assert any("S0C7" in f for f in found) and any("DFHAC2206" in f for f in found)
    assert detect_errors(["READY"]) == []


# -- service -----------------------------------------------------------------
def test_logon_reaches_ready(settings):
    svc, term = make(settings)
    out = svc.logon()
    assert "READY" in out and svc.logged_on
    assert "SECRET1" in term.typed  # password typed into the host, never returned
    assert "SECRET1" not in out


def test_logon_exits_auto_started_ispf(settings):
    svc, _ = make(settings, ispf_on_logon=True)
    assert "READY" in svc.logon()


def test_logon_rejected_when_already_logged_on(settings):
    svc, _ = make(settings, already_logged_on=True)
    with pytest.raises(Exception, match="already logged on"):
        svc.logon()


def test_wrong_password_reported(settings):
    settings.password = "WRONG"
    svc, _ = make(settings)
    with pytest.raises(Exception, match="NOT AUTHORIZED"):
        svc.logon()


def test_list_datasets_pages_through_output(settings):
    svc, term = make(settings)
    out = svc.list_datasets("tester")
    assert out.startswith("$ LISTCAT LEVEL(TESTER)")
    for i in range(30):
        assert f"TESTER.DATA.SET{i:02}" in out
    assert "***" not in out and not out.rstrip().endswith("READY")


def test_list_members(settings):
    svc, _ = make(settings)
    out = svc.list_members("'tester.source.cobol'")
    assert "PAY001" in out and "PAY002" in out


def test_unknown_command_flagged(settings):
    settings.tso_allowlist.append("FOO")
    svc, _ = make(settings)
    assert "DETECTED" in svc.run_tso("FOO")


def test_free_typing_disabled_by_default(settings):
    svc, _ = make(settings)
    svc.logon()
    with pytest.raises(Refused):
        svc.type_text("DELETE X", True)


def test_logoff(settings):
    svc, term = make(settings)
    svc.logon()
    assert "Logged off" in svc.logoff()
    assert not term.connected and "LOGOFF" in term.typed


# -- MCP layer -----------------------------------------------------------------
def _call(mcp, name, args):
    async def go():
        result = await mcp.call_tool(name, args)
        content = result[0] if isinstance(result, tuple) else result
        return content[0].text
    return anyio.run(go)


def test_tools_registered(settings):
    svc, _ = make(settings)
    mcp = build_server(settings, svc)
    names = {t.name for t in anyio.run(mcp.list_tools)}
    assert {"list_datasets", "list_members", "dataset_info", "job_status", "run_tso_command",
            "get_screen", "press_key", "check_for_errors", "mainframe_logon"} <= names
    assert not any("submit" in n or "delete" in n or "edit" in n for n in names)


def test_tool_refusal_and_audit(settings):
    svc, _ = make(settings)
    mcp = build_server(settings, svc)
    assert _call(mcp, "run_tso_command", {"command": "DELETE 'X.Y'"}).startswith("REFUSED:")
    assert "PAY001" in _call(mcp, "list_members", {"dataset": "TESTER.SOURCE.COBOL"})
    rows = [json.loads(l) for l in open(settings.audit_log_path)]
    assert [r["outcome"] for r in rows] == ["refused", "ok"]
    assert "SECRET1" not in open(settings.audit_log_path).read()


def test_password_redacted_from_screen(settings):
    svc, term = make(settings)
    mcp = build_server(settings, svc)
    _call(mcp, "mainframe_logon", {})
    term.lines.append("ECHOED SECRET1 BY HOST")
    assert "SECRET1" not in _call(mcp, "get_screen", {})


def test_http_requires_api_key(settings):
    svc, _ = make(settings)
    app = build_http_app(settings, build_server(settings, svc))
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}
    hdrs = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    with TestClient(app) as client:
        assert client.get("/healthz").text == "ok"
        assert client.post("/mcp", json=init, headers=hdrs).status_code == 401
        r = client.post("/mcp", json=init, headers={**hdrs, "x-api-key": "k3y"})
        assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "mainframe"
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "list_members", "arguments": {"dataset": "TESTER.SOURCE.COBOL"}}}
        r = client.post("/mcp", json=call, headers={**hdrs, "authorization": "Bearer k3y"})
        assert "PAY001" in r.json()["result"]["content"][0]["text"]
