# IT Incident Resolution Agent (Python, LangGraph)

A conversational service-desk agent for fictional Northwind Fabrication. An employee, or an L1 analyst, chats about an existing incident (mostly VPN problems). The agent:

- retrieves the incident
- searches a small knowledge base
- runs a VPN diagnostic against the incident's registered device
- asks follow-up questions
- records work notes and troubleshooting attempts
- updates the incident state
- then either closes the incident with evidence and caller confirmation, or escalates it to a human team

Business requirements: [`docs/business-requirements/itsm-python-langgraph.md`](../../docs/business-requirements/itsm-python-langgraph.md). The workflow is inspired by public descriptions of ITSM AI-agent use cases (ServiceNow Now Assist for ITSM). It is vendor-neutral, and all data is fictional.

## Architecture

```
 CLI (itsm-agent chat)      HTTP API (FastAPI, X-User-Id header)
          \                     /
           ITSMAgentService  (service.py)  -- thread ownership, confirmation resume
                    |
           LangGraph StateGraph (graph.py), SqliteSaver checkpointer (var/checkpoints.db)
     START -> agent --(tool calls?)--> tools (ToolNode) --> agent ... --> END
                                         |
                                         +-- pending confirmation --> await_confirmation (interrupt())
                                                                      resumes with Command(resume=...)
           Tools (tools.py) -- policy.py (deterministic authorization and lifecycle rules)
            |            |                       |
   SQLite var/itsm.db   data/kb_articles.json   VPN diagnostic mock (HTTP, 127.0.0.1:8765)
   incidents, notes,    22 articles, some       scripted pass/fail/timeout/503/malformed per device
   runs, audit, ...     overlapping/outdated
                                                 escalation notifications -> var/outbox/notifications.jsonl
```

- **Graph state** (`ITSMState`): `messages` (with the `add_messages` reducer), `actor_id` (set by the service from the authenticated user, never by the model), `incident_id`, `pending_confirmation`.
- **Agent node**: calls the chat model with the tools bound (`tool_choice` left at the default `auto`). It handles `stop_reason` `refusal` and `max_tokens` by replacing the turn with a safe message and executing none of its tool calls. On `anthropic.APIError` it returns a safe message.
- **Tools node**: the prebuilt `ToolNode`. Tools read the acting user and thread from `ToolRuntime`. Every tool returns JSON with `status` = `ok` | `rejected` | `not_found` | `error`.
- **Confirmation**: `request_caller_confirmation` sets `pending_confirmation` through a `Command`. The graph then routes to `await_confirmation`, which calls `interrupt()`. The caller's yes/no comes back as structured data (CLI prompt or `POST /conversations/{id}/confirmation`), not as model-interpreted chat text. If the session user is not the caller (an analyst), the request is stored as pending and the incident goes `on_hold` instead. A "no" reopens a resolved incident, and that evidence run can no longer be used for resolution.
- **Follow-up questions** are ordinary conversational turns: the agent ends its turn with a question, and the next user message continues the checkpointed thread.
- **Business rules** live in `policy.py` and are enforced inside the tools:
  - authorization by role, caller and assignment group
  - status transitions
  - resolution evidence (the latest diagnostic must pass; timeouts and errors never count)
  - caller confirmation tied to the evidence run
  - mandatory escalation: 2 unsuccessful attempts, 3 unavailable diagnostics in a row, `CERT_REVOKED`/`ACCOUNT_LOCKED`/`GATEWAY_OUTAGE`, or P1
  - P1 cannot be resolved by the agent
- **Records**: every field change writes an `incident_audit` row (actor, `via=itsm-agent`, thread ID). Diagnostic runs (`DR-<incident>-NNN`), confirmations and escalations have their own tables. Each diagnostic also adds a system work note.
- **Logging**: standard `logging` to `var/logs/itsm-agent.log`. It logs one line per model call (stop_reason, tool calls, tokens), one per tool call (actor, thread, status, code, outcome), every diagnostic attempt and every authorization denial.

### Tools

| Tool | Effect |
|---|---|
| `get_incident` | Read the incident, recent notes, diagnostic runs, escalation requirements and the session user's permission |
| `search_knowledge_base` | Keyword-ranked KB search. Retired articles are excluded by default; `review_overdue` and `superseded_by` are flagged |
| `run_vpn_diagnostic` | Calls the local diagnostic service for the incident's `cmdb_ci` (5 s timeout, 1 retry); stores the run and a system note |
| `add_work_note` | Internal note |
| `record_troubleshooting_step` | Note with outcome `successful` / `unsuccessful` / `inconclusive`; counts toward the escalation threshold |
| `update_incident_status` | `in_progress` / `on_hold` (with reason) / reopen; cannot set `resolved` or `closed` |
| `escalate_incident` | Reassigns to an L2 / major-incident group, records the escalation, writes an outbox notification |
| `request_caller_confirmation` | Structured yes/no from the caller (interrupt) |
| `resolve_incident` | Requires the cited run to be the latest diagnostic run and to have passed, and the caller not to have declined it |
| `close_incident` | Requires `resolved` state, evidence still current and a matching caller confirmation (unless the incident does not require one) |

## Install

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 or 3.13 (pinned to 3.12 in `.python-version`).

```bash
cd agents/itsm-python-langgraph
uv sync                     # creates .venv from uv.lock
cp .env.example .env        # optional; defaults work for fixture mode
```

## Configure

All settings come from environment variables (or `.env`). See [`.env.example`](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `ITSM_MODEL_MODE` | `fixture` | `fixture` (scripted model) or `real` (Claude) |
| `ITSM_FIXTURE_SCRIPT` | none | Scenario JSON whose `model_script` the fixture model replays (required in fixture mode) |
| `ANTHROPIC_API_KEY` | none | Required in real mode (`ANTHROPIC_BASE_URL` is honoured by the SDK) |
| `MODEL` | `claude-opus-5-5` | Model ID |
| `MODEL_EFFORT` | `medium` | Sent as `output_config.effort` |
| `MODEL_MAX_TOKENS` | `16000` | |
| `DIAG_SERVICE_URL` | `http://127.0.0.1:8765` | Diagnostic mock |
| `DIAG_TIMEOUT_S` / `DIAG_RETRIES` | `5.0` / `1` | Client timeout per attempt, retries |
| `ITSM_DB_PATH`, `ITSM_CHECKPOINT_DB_PATH`, `ITSM_OUTBOX_PATH`, `ITSM_LOG_PATH` | under `var/` | Storage |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | none | Turns on OpenTelemetry traces over OTLP/HTTP (other `OTEL_*` variables apply). Off when unset |

**Traces** (`telemetry.py`): one `execute_tool <tool>` span per tool call, following OpenTelemetry's GenAI conventions, with the tool name and a SHA-256 of its arguments (never the arguments). A failed or rejected call is an ERROR span with `error.type`. Business outcomes are span events: `resolution_confirmed`, `resolution_declined`, `incident_resolved`, `incident_closed`, `incident_escalated`. They are declared, with the tools and stores, in [`evalgo.yaml`](evalgo.yaml).

Real mode builds `ChatAnthropic(model=MODEL, max_tokens=16000, effort=MODEL_EFFORT, timeout=120, max_retries=2)`. No `temperature`, `top_p` or `top_k` is sent, and there is no assistant prefill. Thinking is adaptive by default on this model (langchain-anthropic adds `thinking: adaptive`). Tool choice is `auto`. A test (`test_real_mode_model_configuration`) checks the request payload without calling the API.

## Start

```bash
uv run itsm-agent seed                       # (re)create var/itsm.db with fictional seed data
uv run itsm-agent diag-service               # terminal 1: VPN diagnostic mock on :8765

# terminal 2, fixture mode (scripted model replaying the happy-path scenario)
ITSM_MODEL_MODE=fixture ITSM_FIXTURE_SCRIPT=fixtures/scenarios/vpn_happy_path.json \
  uv run itsm-agent chat --user u-1001 --incident INC-1001 -v

# real mode
ITSM_MODEL_MODE=real ANTHROPIC_API_KEY=... uv run itsm-agent chat --user u-1001 --incident INC-1001 -v

uv run itsm-agent show INC-1001              # incident, notes, runs, confirmations, escalations, audit as JSON
uv run itsm-agent api --port 8000            # HTTP API
```

Resume a conversation with `--thread <id>`. Checkpoints persist in `var/checkpoints.db`. You can also query `var/itsm.db` directly with `sqlite3`.

### HTTP API

Identity comes from the `X-User-Id` header. This is a stand-in for an SSO proxy, so do not expose the API beyond localhost.

| Method and path | Body | Purpose |
|---|---|---|
| `GET /health` | | |
| `POST /conversations` | `{"incident_id": "INC-1001"}` | Start a thread (201, `thread_id`) |
| `POST /conversations/{id}/messages` | `{"message": "..."}` | One turn. Returns `reply`, `tool_calls`, `pending_confirmation` |
| `POST /conversations/{id}/confirmation` | `{"confirmed": true, "comment": "..."}` | Answer a pending caller confirmation |
| `GET /conversations/{id}` | | Checkpointed transcript |
| `GET /incidents/{number}` | | Incident snapshot (subject to read permission) |

Errors: 401 unknown user, 403 forbidden or inactive user or another user's thread, 404, 409 when a confirmation is pending or there is none to answer, 422 missing header.

## Seed data (fictional)

Twelve users cover the main cases: employees, one inactive employee, a contractor, a service-desk analyst, a network engineer, an auditor and a monitoring service account. Twelve incidents cover:

| Incident | Case |
|---|---|
| INC-1001 | Happy path (outdated client, then passes) |
| INC-1002 | Expired certificate that keeps failing (leads to escalation) |
| INC-1003 | Diagnostic service times out |
| INC-1004 | Outlook, not a VPN issue |
| INC-1005 | P1 gateway outage |
| INC-1006 | Already closed |
| INC-1007 | Already escalated to L2 |
| INC-1008 | Inactive caller |
| INC-1009 | Contractor (MFA) |
| INC-1010 | Revoked certificate (security escalation) |
| INC-1011 | Hotel Wi-Fi: 503 then pass, on hold awaiting caller |
| INC-1012 | Monitoring-generated, no caller confirmation required |

Device behaviour is in `data/diagnostic_profiles.json`. The KB (`data/kb_articles.json`, 22 articles) includes:

- superseded articles (KB0010002 to KB0010001, KB0010008 to KB0010009)
- one retired article (KB0010004)
- a conflicting pair (KB0010010 says to lower the MTU; KB0010011 says not to change adapter settings)
- articles overdue for review

## Tests

```bash
uv run pytest                      # everything
uv run pytest -m "not integration" # unit/component: policy, KB, diagnostic client vs mock HTTP server, repository, config
uv run pytest -m integration       # real graph + tools + SQLite + diagnostic HTTP mock with scripted model; API + CLI e2e
uv run python scripts/run_fixture_demo.py   # regenerate samples/
```

Last run (2026-11-14, macOS, Python 3.12.13): **91 passed** in about 39 s. That is 57 unit/component tests and 34 integration tests, including 4 end-to-end API/CLI tests (one runs the CLI chat as a subprocess).

## Real mode vs fixture mode

- **Fixture mode** (`ScriptedChatModel`, a `BaseChatModel` subclass in `fixture_model.py`) replays scripted assistant turns. The graph, ToolNode, tools, policy checks, SQLite, checkpointer, interrupts and the HTTP diagnostic mock all run for real. **Fixture mode proves application logic, not LLM quality.** Some scripts deliberately include bad model decisions, such as resolving on a timeout or making a third attempt after the threshold, to show the code-level guards rejecting them. One script serves one conversation per process: the model's position is not tracked per thread.
- **Real mode** uses Claude through `langchain-anthropic`, and the model chooses which tools to call. **Real mode has not been run by this team** (no credentials were available in the build environment).

## Sample walkthrough

`samples/` contains three fixture runs produced by `scripts/run_fixture_demo.py`. Each has `conversation.md`, `conversation.json` (including the checkpointed transcript), `state_before.json`, `state_after.json`, the application log `itsm-agent.log` and, where relevant, `outbox_notifications.jsonl`.

- `vpn_happy_path`:
  1. Diagnostic `DR-INC-1001-001` fails with `CLIENT_OUTDATED`, and the agent sends the employee the KB0010001 steps.
  2. The employee reports the update is done. Diagnostic `DR-INC-1001-002` passes.
  3. The structured confirmation pause runs, and the caller says yes.
  4. The agent resolves the incident with `evidence_run_id=DR-INC-1001-002`, then closes it.
  5. The audit shows `new -> in_progress -> resolved -> closed`.
- `vpn_escalation_after_failures` (error/escalation case): two `unsuccessful` attempts. The third attempt is rejected with `escalation_required`, and the incident is escalated to Identity & Access L2 with an outbox notification.
- `vpn_diagnostic_timeout` (tool-failure case): three diagnostic runs time out (each tried twice). `resolve_incident` is rejected with `evidence_not_passing` even though the employee says "seems better", and the incident is escalated to Network Operations L2. The state never becomes `resolved`.

## Known limitations

- Real-model mode is implemented but **not executed**. Prompt quality and tool-selection behaviour with Claude are unverified.
- Authentication is a header or CLI flag (mock SSO). There is no real identity provider.
- The diagnostic mock advances a per-device script on every call, including retries. It does not model the employee's actual actions, and its state resets when the service restarts.
- Policy checks for resolve/close run before the write transaction opens, not inside it. With two parallel tool calls on the same incident, a race is theoretically possible.
- ToolNode runs parallel tool calls from one model turn concurrently. The prompt and tool description ask for `request_caller_confirmation` on its own, but this is not enforced.
- Fixture model: one linear script per process, with no branching on tool results.
- The KB search is lexical (TF-IDF-like), not semantic.
- Out of scope: incident creation, SLA timers, auto-close after N days, real notification delivery (the outbox is a local JSONL file).
- `langsmith` is installed as a transitive dependency of `langchain-core`. Tracing stays off unless `LANGSMITH_*` env vars are set.

## Dependency licenses

| Package | Version | License |
|---|---|---|
| langgraph | 1.2.22 | MIT |
| langgraph-checkpoint / -checkpoint-sqlite / -prebuilt | 4.3.2 / 3.1.3 / 1.1.2 | MIT |
| langchain-core | 1.8.0 | MIT |
| langchain-anthropic | 1.7.10 | MIT |
| anthropic (official SDK, via langchain-anthropic) | 2.8.0 | Apache-2.0 |
| fastapi / starlette | 0.162.7 / 1.10.0 | MIT / BSD-3-Clause |
| uvicorn | 0.54.0 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| python-dotenv | 1.2.4 | BSD-3-Clause |
| pydantic | 2.14.3 | MIT |
| opentelemetry-api / -sdk / -exporter-otlp-proto-http | 1.45.2 | Apache-2.0 |
| pytest (dev) | 9.2.0 | MIT |

Exact pins are in `uv.lock`.

## References

- LangGraph persistence and checkpointers: https://docs.langchain.com/oss/python/langgraph/persistence, https://docs.langchain.com/oss/python/langgraph/checkpointers
- LangGraph Graph API (StateGraph, MessagesState, conditional edges): https://docs.langchain.com/oss/python/langgraph/graph-api
- `interrupt()` / `Command(resume=...)`, `ToolNode`, `ToolRuntime`, `tools_condition`, `SqliteSaver`: docstrings in the installed sources (`langgraph/types.py`, `langgraph/prebuilt/tool_node.py`, `langgraph/checkpoint/sqlite/__init__.py`)
- ChatAnthropic (effort, bind_tools, max_tokens, response metadata): https://docs.langchain.com/oss/python/integrations/chat/anthropic, plus the installed `langchain_anthropic/chat_models.py`
- Anthropic Python SDK (errors, stop reasons, effort, tool use): https://github.com/anthropics/anthropic-sdk-python and the Claude API reference notes for Python
- Package versions: https://pypi.org/project/langgraph/, https://pypi.org/project/langchain-anthropic/
- Workflow inspiration: https://www.servicenow.com/docs/r/xanadu/it-service-management/now-assist-for-it-service-management-itsm/now-assist-itsm-ai-agents-use-cases.html
