# mf-mcp: read-only mainframe MCP server (TN3270)

This server lets a Copilot Studio agent, or any other MCP client, read from an IBM z/OS mainframe using only a **TN3270** connection. It doesn't need z/OSMF, Zowe or FTP. It logs on to TSO with a service userid and runs a fixed list of read-only TSO commands. It returns screen text the agent can reason over.

It is written from scratch. The only libraries it uses are IBM's [`tnz`](https://github.com/IBM/tnz) TN3270 client (Apache-2.0) and the official MCP Python SDK (MIT). It contains no AGPL code.

## Tools

| Tool | What it does | TSO command used |
|---|---|---|
| `list_datasets(prefix)` | Datasets under a high-level qualifier | `LISTCAT LEVEL(prefix)` |
| `list_members(dataset)` | PDS members plus RECFM/LRECL/DSORG | `LISTDS 'dsn' MEMBERS` |
| `dataset_info(dataset)` | Dataset attributes | `LISTDS 'dsn'` |
| `job_status(jobname)` | JES status of a job | `STATUS jobname` |
| `run_tso_command(command)` | Any **allow-listed** TSO command | as given |
| `get_screen()` | Current 3270 screen, row-numbered | – |
| `press_key(key)` | ENTER, CLEAR, PF1–24, PA1–3 | – |
| `check_for_errors()` | Finds abends (S0C7, U4038, ASRA…), RACF denials and TSO errors | – |
| `type_text(text)` | Free typing. **Off by default.** | – |
| `mainframe_status / mainframe_logon / mainframe_logoff` | Session control | – |

The tools log on automatically, so the agent can call `list_datasets` straight away.

## Safety
- **Read-only.** There are no write, submit or delete tools. `run_tso_command` refuses any verb that isn't in `MF_TSO_ALLOWLIST` (default: `LISTCAT, LISTDS, LISTALC, STATUS, TIME`). Dataset names and jobnames are checked so commands can't be smuggled in.
- **Credentials stay on the server.** The userid and password come from environment variables, which you can fill from Key Vault. The agent never sees or supplies them, and the password is masked in every response and audit row.
- **API key on HTTP.** Every request needs an `x-api-key` header (or `Authorization: Bearer`). Only `/healthz` is open.
- Every call is written to an audit log (JSON lines). Calls are rate-limited (60 per minute by default).
- **Least privilege.** Give the service userid RACF **READ** access only to the datasets the agent should see. RACF is the real boundary; this server adds a second layer on top.

## Configuration (environment variables)

| Variable | Default | Notes |
|---|---|---|
| `MF_HOST`, `MF_PORT` | –, `23` | TN3270 endpoint (usually `992` with TLS) |
| `MF_TLS`, `MF_VERIFY_CERT` | `false`, `true` | Use TLS in production |
| `MF_USERID`, `MF_PASSWORD` | – | Service account. Keep the password in a secret store |
| `MF_APPLID` | `TSO` | Text typed on the VTAM welcome screen; `{userid}` is replaced, e.g. `TSO {userid}`. Leave it empty if the host goes straight to TSO |
| `MF_TSO_ALLOWLIST` | see above | Comma-separated TSO verbs |
| `MF_ALLOW_FREE_TYPING` | `false` | Turns on `type_text`. Leave it off for read-only use |
| `MF_COMMAND_TIMEOUT` | `20` | Seconds to wait for each screen |
| `MF_CALLS_PER_MINUTE` | `60` | |
| `MF_AUDIT_LOG` | `./logs/audit.jsonl` | |
| `MCP_API_KEY` | – | **Required** for HTTP |
| `PORT` | `8000` | HTTP port |

## Run locally

```bash
pip install -e ".[dev]"
pytest -q                                   # 26 offline tests, no mainframe needed
cp .env.example .env && edit .env
set -a; . ./.env; set +a
mf-mcp --transport http                     # MCP endpoint: http://localhost:8000/mcp
```

## Deploy and connect to Copilot Studio

1. **Host it where it can reach the mainframe.** Copilot Studio calls the server from Microsoft's cloud, so the server needs a public **HTTPS** URL. It also needs a network path to your TN3270 port. The usual setup is Azure Container Apps or App Service with VNet integration, linked to on-premises over ExpressRoute or VPN. Build the container with the included `Dockerfile` and put the secrets in Key Vault.
2. **Add the server in Copilot Studio.** Open your agent, then **Tools → Add a tool → New tool → Model Context Protocol**.
   - Server URL: `https://<your-host>/mcp`
   - Authentication: **API key**, sent as a **header** named `x-api-key`. Enter the value of `MCP_API_KEY` when you create the connection.
3. **Turn on the tools** you want. In the agent instructions, add a line like: *"Use the mainframe tools to answer questions about datasets, members and job status. Report REFUSED/ERROR results as-is."*
4. **Test it** with *"List the members of APP.SOURCE.COBOL"* or *"What is the status of job PAYJOB1?"*

The Copilot Studio menu names may differ a little between releases. The required pieces stay the same: a Streamable HTTP MCP URL and an API-key header.

## Things to know
- There is **one shared TSO session**, so calls run one at a time. Copilot Studio's per-call latency is usually fine for this. For many concurrent users, run a pool of servers, each with its own userid.
- If the service userid stays logged on elsewhere, TSO logon fails with `IKJ56425I`. Use a dedicated userid that nobody logs on with interactively.
- Logon handles the common screens: VTAM welcome, `ENTER USERID`, the TSO/E logon panel, `***` continuation, and an ISPF menu that starts automatically. If your site's screens look different, adjust `MF_APPLID` or the patterns at the top of `service.py`.

## Next steps (not built yet)
- `read_member`: read source and JCL by browsing it in ISPF and scrolling with PF8.
- `fetch_job_output`: read SYSOUT through SDSF or TSO `OUTPUT`.
- Write tools (edit or submit) with an approval step, only if you decide you need them.
- If z/OSMF or z/OS Connect becomes available, switch the transport behind the same tool names.
