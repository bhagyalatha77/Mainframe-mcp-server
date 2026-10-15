"""Configuration loaded from environment variables.

Secrets (mainframe password, API key) come from the environment, which in
Azure is typically populated from Key Vault. They are never accepted as tool
arguments, so the agent never sees them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return default
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


# Read-only TSO commands the agent may run. Anything else is refused.
DEFAULT_TSO_ALLOWLIST = ["LISTCAT", "LISTC", "LISTDS", "LISTD", "LISTALC", "STATUS", "ST", "TIME"]


@dataclass(slots=True)
class Settings:
    host: str = ""
    port: int = 23
    tls: bool = False
    verify_cert: bool = True
    userid: str = ""
    password: str = field(default="", repr=False)
    # VTAM application to type on the first screen (e.g. "TSO"). Empty = skip.
    applid: str = "TSO"
    connect_timeout: float = 30.0
    command_timeout: float = 20.0
    tso_allowlist: list[str] = field(default_factory=lambda: list(DEFAULT_TSO_ALLOWLIST))
    allow_free_typing: bool = False
    calls_per_minute: int = 60
    audit_log_path: str = "./logs/audit.jsonl"
    # HTTP transport
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.environ.get("MF_HOST", ""),
            port=int(os.environ.get("MF_PORT", "23")),
            tls=_bool("MF_TLS", False),
            verify_cert=_bool("MF_VERIFY_CERT", True),
            userid=os.environ.get("MF_USERID", "").upper(),
            password=os.environ.get("MF_PASSWORD", ""),
            applid=os.environ.get("MF_APPLID", "TSO"),
            connect_timeout=float(os.environ.get("MF_CONNECT_TIMEOUT", "30")),
            command_timeout=float(os.environ.get("MF_COMMAND_TIMEOUT", "20")),
            tso_allowlist=_list("MF_TSO_ALLOWLIST", list(DEFAULT_TSO_ALLOWLIST)),
            allow_free_typing=_bool("MF_ALLOW_FREE_TYPING", False),
            calls_per_minute=int(os.environ.get("MF_CALLS_PER_MINUTE", "60")),
            audit_log_path=os.environ.get("MF_AUDIT_LOG", "./logs/audit.jsonl"),
            http_host=os.environ.get("MCP_HTTP_HOST", "0.0.0.0"),
            http_port=int(os.environ.get("PORT", os.environ.get("MCP_HTTP_PORT", "8000"))),
            api_key=os.environ.get("MCP_API_KEY", ""),
        )
