"""MCP server exposing read-only mainframe tools over stdio or Streamable HTTP.

Run:
    python -m mf_mcp.server --transport http    # for Copilot Studio / remote agents
    python -m mf_mcp.server --transport stdio   # for local MCP clients
"""
from __future__ import annotations

import argparse
import hmac
import logging
from typing import Callable

import anyio
from mcp.server.fastmcp import FastMCP

from .config import Settings
from .safety import AuditLog, RateLimiter, Refused
from .service import MainframeService
from .terminal import TerminalError, TnzTerminal

log = logging.getLogger("mf_mcp")

INSTRUCTIONS = """Read-only access to an IBM z/OS mainframe over TN3270 (TSO).
Typical flow: call list_datasets / list_members / dataset_info / job_status directly;
they log on automatically. Use get_screen and press_key only to inspect or page
through the raw 3270 screen. Results starting with REFUSED: were blocked by policy;
ERROR: means the host or connection failed. Never claim an action succeeded if it
returned REFUSED or ERROR."""


def build_server(settings: Settings, service: MainframeService | None = None) -> FastMCP:
    if service is None:
        terminal = TnzTerminal(settings.host, settings.port, settings.tls,
                               settings.verify_cert, settings.connect_timeout)
        service = MainframeService(settings, terminal)
    audit = AuditLog(settings.audit_log_path, [settings.password, settings.api_key])
    limiter = RateLimiter(settings.calls_per_minute)

    mcp = FastMCP("mainframe", instructions=INSTRUCTIONS, host=settings.http_host,
                  port=settings.http_port, stateless_http=True, json_response=True)

    async def call(tool: str, args: dict, fn: Callable[[], str]) -> str:
        def guarded() -> str:
            with service.lock:
                return fn()
        try:
            limiter.check()
            result = await anyio.to_thread.run_sync(guarded)
            audit.write(tool, args, "ok")
            return audit.redact(result)
        except Refused as exc:
            audit.write(tool, args, "refused", str(exc))
            return f"REFUSED: {exc}"
        except (TerminalError, OSError, ValueError) as exc:
            audit.write(tool, args, "error", str(exc))
            return audit.redact(f"ERROR: {exc}")
        except Exception as exc:  # keep the server alive; report to caller
            log.exception("%s failed", tool)
            audit.write(tool, args, "error", repr(exc))
            return audit.redact(f"ERROR: unexpected failure in {tool}: {exc}")

    @mcp.tool()
    async def mainframe_status() -> str:
        """Show connection state: host, userid, connected and logged-on flags."""
        return await call("mainframe_status", {}, service.status)

    @mcp.tool()
    async def mainframe_logon() -> str:
        """Connect and log on to TSO with the server's configured service userid.

        Usually not needed: other tools log on automatically. Returns the READY screen.
        """
        return await call("mainframe_logon", {}, service.logon)

    @mcp.tool()
    async def mainframe_logoff() -> str:
        """Log off TSO and close the TN3270 connection."""
        return await call("mainframe_logoff", {}, service.logoff)

    @mcp.tool()
    async def list_datasets(prefix: str) -> str:
        """List catalogued datasets under a high-level qualifier, e.g. prefix='PROD.PAYROLL'.

        Runs TSO LISTCAT LEVEL(prefix).
        """
        return await call("list_datasets", {"prefix": prefix}, lambda: service.list_datasets(prefix))

    @mcp.tool()
    async def list_members(dataset: str) -> str:
        """List members of a partitioned dataset (PDS/PDSE), e.g. dataset='APP.SOURCE.COBOL'.

        Also shows RECFM, LRECL, BLKSIZE and DSORG. Runs TSO LISTDS 'dataset' MEMBERS.
        """
        return await call("list_members", {"dataset": dataset}, lambda: service.list_members(dataset))

    @mcp.tool()
    async def dataset_info(dataset: str) -> str:
        """Show attributes (RECFM, LRECL, BLKSIZE, DSORG, volume) of one dataset."""
        return await call("dataset_info", {"dataset": dataset}, lambda: service.dataset_info(dataset))

    @mcp.tool()
    async def job_status(jobname: str) -> str:
        """Show JES status of batch jobs by name, e.g. 'PAYJOB1' or 'PAYJOB1(JOB01234)'.

        Runs TSO STATUS. Many sites only allow jobs whose names start with the userid.
        """
        return await call("job_status", {"jobname": jobname}, lambda: service.job_status(jobname))

    @mcp.tool()
    async def run_tso_command(command: str) -> str:
        """Run one read-only TSO command and return its output.

        Only allow-listed commands run (default: LISTCAT, LISTDS, LISTALC, STATUS, TIME).
        Others return REFUSED.
        """
        return await call("run_tso_command", {"command": command}, lambda: service.run_tso(command))

    @mcp.tool()
    async def get_screen() -> str:
        """Return the current 3270 screen as row-numbered text (24 rows by default)."""
        return await call("get_screen", {}, service.get_screen)

    @mcp.tool()
    async def press_key(key: str) -> str:
        """Press a 3270 key and return the new screen. key: ENTER, CLEAR, PF1-PF24 or PA1-PA3.

        PF7/PF8 usually scroll up/down; PF3 goes back; PA1 interrupts output.
        """
        return await call("press_key", {"key": key}, lambda: service.press_key(key))

    @mcp.tool()
    async def type_text(text: str, press_enter: bool = True) -> str:
        """Type text at the cursor (and optionally press ENTER). Disabled unless the operator enabled it."""
        return await call("type_text", {"text": text, "press_enter": press_enter},
                          lambda: service.type_text(text, press_enter))

    @mcp.tool()
    async def check_for_errors() -> str:
        """Scan the current screen for abends (S0C7, U4038, ASRA...), RACF denials and TSO errors."""
        return await call("check_for_errors", {}, service.check_for_errors)

    return mcp


class ApiKeyMiddleware:
    """Pure-ASGI API-key check (keeps streaming intact). /healthz stays open."""

    def __init__(self, app, api_key: str):
        self.app = app
        self.api_key = api_key.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/healthz":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        supplied = headers.get(b"x-api-key", b"")
        auth = headers.get(b"authorization", b"")
        if auth.lower().startswith(b"bearer "):
            supplied = supplied or auth[7:].strip()
        if not hmac.compare_digest(supplied, self.api_key):
            body = b'{"error":"unauthorized"}'
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        return await self.app(scope, receive, send)


def build_http_app(settings: Settings, mcp: FastMCP):
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    app = mcp.streamable_http_app()
    app.router.routes.append(Route("/healthz", lambda request: PlainTextResponse("ok")))
    if not settings.api_key:
        raise SystemExit("MCP_API_KEY must be set for the HTTP transport")
    return ApiKeyMiddleware(app, settings.api_key)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only mainframe MCP server (TN3270)")
    parser.add_argument("--transport", choices=["http", "stdio"], default="http")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    settings = Settings.from_env()
    mcp = build_server(settings)
    if args.transport == "stdio":
        mcp.run("stdio")
        return
    import uvicorn
    uvicorn.run(build_http_app(settings, mcp), host=settings.http_host, port=settings.http_port)


if __name__ == "__main__":
    main()
