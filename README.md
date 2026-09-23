# Resume Matching Agent — MCP Migration

Migrates the Milestone 1 resume-matching agent from hand-rolled filesystem
tools to a standards-based **Model Context Protocol (MCP)** server, and
refactors the agent to consume it via an MCP client inside a LangGraph
state machine.

## Files

| File                        | Purpose |
|-----------------------------|---------|
| `filesystem_mcp_server.py`  | Part A — MCP server exposing filesystem/resume tools |
| `config.py`                 | Server config + path-security boundary |
| `mcp_errors.py`             | JSON-RPC-flavored structured error types |
| `matching_agent.py`         | Part B — LangGraph agent, MCP client only, no direct file IO |
| `test_mcp_server.py`        | 8 integration test scenarios over real stdio/JSON-RPC |
| `workflow_diagram.md`       | State machine + sequence diagrams (Mermaid) |
| `DEMO_VIDEO_SCRIPT.md`      | Shot-by-shot script for the 5–6 min demo video |
| `requirements.txt`          | Pinned dependencies |

## Setup

```bash
pip install -r requirements.txt
mkdir -p resumes
export MCP_ALLOWED_DIRS=$(pwd)/resumes   # security boundary for the server
export ANTHROPIC_API_KEY=sk-...          # only needed for the agent's match step
```

Drop some `.txt`, `.md`, `.pdf`, or `.docx` resumes into `resumes/`.

## Run the tests (proves MCP + JSON-RPC compliance)

```bash
pytest test_mcp_server.py -v
```

## Run the server standalone

```bash
python filesystem_mcp_server.py
```

It speaks JSON-RPC 2.0 over stdio and waits for a client — this is normal;
`matching_agent.py` spawns it automatically, you don't run it separately in
normal use.

## Run the agent end-to-end

```bash
python matching_agent.py \
  --directory resumes \
  --job-text "Backend engineer, 3+ years Python, Kubernetes experience" \
  --output resumes/match_results.json
```

## What changed from Milestone 1

- **Before:** `matching_agent.py` called `os.listdir`, `open()`, and PDF
  parsing libraries directly.
- **After:** every filesystem operation is an MCP tool call
  (`list_resumes`, `read_resume`, `batch_process`, `watch_directory`,
  `search_resumes`, `get_file_metadata`, `write_output`), discovered at
  runtime via `tools/list` rather than hardcoded imports.
- **New capabilities** not present in Milestone 1: `watch_directory()` for
  polling a folder for new resumes, and `batch_process()` for concurrent
  multi-file processing in a single round trip.
- **Bonus:** the agent's MCP config is a dict of servers, so a second server
  (e.g. web search) can be added without changing any graph logic — see
  `with_web_search_server()`.

## Design notes / assumptions

- "Milestone 1 tools" were assumed to be: list resumes in a directory, read
  a resume's text, get file metadata, write a results file, and keyword
  search across resumes — the common core of a resume-screening tool set.
  If your Milestone 1 code exposed different tools, add them to
  `filesystem_mcp_server.py` following the same pattern (`@mcp.tool()` +
  route through `_safe_path()`).
- Security: all paths are resolved and checked against `MCP_ALLOWED_DIRS`
  before any disk access, to block path-traversal from a compromised or
  buggy MCP client.
- `watch_directory()` persists last-seen file mtimes to a small JSON state
  file (`.mcp_watch_state.json`) because MCP tool calls are stateless
  request/response — there's no built-in "subscribe and get pushed events"
  in this setup, so polling with a diff is the honest way to do it over
  stdio.
