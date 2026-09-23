# Demo Video Script (5–6 minutes)

Record your screen (terminal + editor) while narrating. Suggested timing below;
adjust to your pace but keep total under 6 minutes.

## 0:00–0:45 — Intro & architecture
- State the goal: "Milestone 1 had a resume-matching agent with hand-rolled
  filesystem tools. This milestone replaces those with a standards-based MCP
  server, and refactors the agent into an MCP client."
- Show `workflow_diagram.md` rendered (sequence diagram) as the mental model:
  Agent → MCP Client → MCP Server (JSON-RPC 2.0) → filesystem, and Agent → Claude.

## 0:45–2:00 — MCP server walkthrough (Part A)
- Open `filesystem_mcp_server.py`. Point out:
  - `@mcp.tool()` decorators = Milestone 1 tools now exposed via MCP (`list_resumes`,
    `read_resume`, `search_resumes`, `write_output`, `get_file_metadata`).
  - The two **new** MCP-specific tools: `watch_directory()` and `batch_process()`.
  - `mcp_errors.py`: structured JSON-RPC error codes instead of raw exceptions.
  - `config.py`: `resolve_and_validate()` — the path-traversal security boundary.
- Run the server standalone for a second to show it starts cleanly:
  `python filesystem_mcp_server.py` (Ctrl+C after a moment — stdio servers
  wait for a client, so this just proves it boots without errors).

## 2:00–3:00 — Resource discovery & JSON-RPC compliance
- Run `pytest test_mcp_server.py -v` live and let it finish. Narrate the
  scenarios as they scroll:
  - discovery (`tools/list`), list+read, keyword search, batch_process,
    watch_directory detecting a newly dropped file, path-traversal rejection,
    missing-file structured error, overwrite-guard on write_output.
- Call out that this is the **real MCP wire protocol** over stdio — not a
  mocked function call — so passing here proves JSON-RPC 2.0 compliance.

## 3:00–4:15 — Agent refactor (Part B)
- Open `matching_agent.py`. Point out:
  - No `open()`/`os.listdir()`/PDF parsing left in this file — only
    `self._call_tool(...)`.
  - `build_mcp_config()` — server address/command lives in one place.
  - The LangGraph `StateGraph`: `discover → extract → match → save`, with a
    conditional edge that short-circuits to `END` if no resumes are found.
- Run it live end-to-end against the sample `resumes/` folder:
  ```
  export ANTHROPIC_API_KEY=sk-...
  python matching_agent.py --directory resumes --job-text "Backend engineer, Python, Kubernetes" --output resumes/match_results.json
  ```
- Show the printed ranked matches and open `match_results.json`.

## 4:15–5:00 — Bonus: multi-MCP integration
- Open `with_web_search_server()` in `matching_agent.py`. Explain: pass a
  second server's stdio command and its tools appear in `tools_by_name`
  automatically — no new agent code needed, only a new entry in the MCP
  config dict. (If you have a real web-search MCP server available, run this
  live; otherwise walk through the code and explain the extension point.)

## 5:00–5:45 — Wrap-up
- Recap what MCP bought you: standardized discovery (`tools/list`), a single
  error/status convention, and the ability to swap the filesystem
  implementation (e.g. move it to S3 or a database) without touching
  `matching_agent.py` at all.
- Mention the security boundary (`resolve_and_validate`) as a production
  concern MCP doesn't remove — you still have to enforce it yourself.

## Checklist before recording
- [ ] `pip install -r requirements.txt`
- [ ] `export ANTHROPIC_API_KEY=...`
- [ ] `export MCP_ALLOWED_DIRS=$(pwd)/resumes`
- [ ] Sample resumes present in `resumes/`
- [ ] `pytest test_mcp_server.py -v` passes before you hit record
