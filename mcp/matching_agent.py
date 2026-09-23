"""
matching_agent.py
==================
Milestone 2: the resume-matching agent, refactored from Milestone 1's
direct/custom filesystem tools onto a LangGraph state machine that talks to
`filesystem_mcp_server.py` exclusively through an MCP client.

What changed vs. Milestone 1:
- No `os.listdir` / `open()` / PDF-parsing code lives in this file anymore.
  Every filesystem operation is now an MCP tool call.
- The agent doesn't know or care whether the MCP server is a local stdio
  process, a remote HTTP service, or swapped for a different implementation
  entirely — it only depends on the MCP tool contract (name + JSON schema).
- Bonus (Part B.2): the agent is wired to accept *multiple* MCP servers at
  once (filesystem + an optional second server, e.g. web search) via
  `MultiServerMCPClient`, and picks tools by capability rather than by
  which server happens to expose them.

Run:
    export ANTHROPIC_API_KEY=sk-...
    python matching_agent.py --directory ./resumes --job job_description.txt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from anthropic import Anthropic
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, StateGraph

MODEL = "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Agent state
# ---------------------------------------------------------------------------
class AgentState(TypedDict, total=False):
    directory: str
    job_description: str
    output_path: str
    resume_files: list[dict]
    extracted_texts: dict[str, str]
    ranked_matches: list[dict]
    errors: Annotated[list[str], lambda a, b: a + b]
    status: str


# ---------------------------------------------------------------------------
# MCP server configuration
# ---------------------------------------------------------------------------
def build_mcp_config(extra_servers: dict[str, dict] | None = None) -> dict:
    """
    Describes every MCP server this agent can draw tools from.

    `filesystem` is required (Part A's server). `extra_servers` lets the
    bonus multi-MCP scenario plug in additional servers (e.g. a web-search
    or database MCP server) without touching the graph logic below — new
    tools just show up in `tools_by_name` if the server exposes them.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    config = {
        "filesystem": {
            "transport": "stdio",
            "command": "python",
            "args": [os.path.join(here, "filesystem_mcp_server.py")],
            "env": {**os.environ},
        }
    }
    if extra_servers:
        config.update(extra_servers)
    return config


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
class MatchingAgent:
    def __init__(self, mcp_config: dict, anthropic_api_key: str | None = None):
        self.mcp_config = mcp_config
        self.client: MultiServerMCPClient | None = None
        self.tools_by_name: dict[str, Any] = {}
        self.anthropic = Anthropic(api_key=anthropic_api_key)
        self.graph = self._build_graph()

    # -- setup ---------------------------------------------------------
    async def _ensure_tools_loaded(self) -> None:
        if self.client is None:
            self.client = MultiServerMCPClient(self.mcp_config)
        if not self.tools_by_name:
            tools = await self.client.get_tools()
            self.tools_by_name = {t.name: t for t in tools}

    async def _call_tool(self, name: str, **kwargs) -> dict:
        """Call an MCP tool by name and parse its JSON payload."""
        await self._ensure_tools_loaded()
        if name not in self.tools_by_name:
            return {"status": "error", "error": {"message": f"Tool '{name}' not available from any connected MCP server"}}
        raw = await self.tools_by_name[name].ainvoke(kwargs)

        # langchain-mcp-adapters returns a list of MCP content blocks
        # (e.g. [{"type": "text", "text": "<json>"}]) rather than a bare
        # string; normalize every shape down to a parsed dict.
        text_payload: str | None = None
        if isinstance(raw, str):
            text_payload = raw
        elif isinstance(raw, list) and raw:
            first = raw[0]
            if isinstance(first, dict) and "text" in first:
                text_payload = first["text"]
            elif hasattr(first, "text"):
                text_payload = first.text
        elif isinstance(raw, dict):
            return raw

        if text_payload is None:
            return {"status": "error", "error": {"message": f"Unrecognized tool response shape: {raw!r}"}}
        try:
            return json.loads(text_payload)
        except json.JSONDecodeError:
            return {"status": "ok", "raw": text_payload}

    # -- graph nodes -----------------------------------------------------
    async def discover_resumes(self, state: AgentState) -> AgentState:
        """Node 1: ask the filesystem MCP server what resumes exist."""
        result = await self._call_tool("list_resumes", directory=state["directory"])
        if result.get("status") != "ok":
            return {"errors": [f"discover_resumes failed: {result.get('error')}"], "status": "failed"}
        return {"resume_files": result["files"], "status": "discovered"}

    async def extract_content(self, state: AgentState) -> AgentState:
        """Node 2: batch_process instead of N sequential read_resume calls —
        this is where MCP's batch_process tool pays for itself."""
        result = await self._call_tool(
            "batch_process", directory=state["directory"], operation="extract_text"
        )
        if result.get("status") != "ok":
            return {"errors": [f"extract_content failed: {result.get('error')}"], "status": "failed"}
        errors = [f"{path}: {err}" for path, err in result.get("errors", {}).items()]
        return {
            "extracted_texts": result["results"],
            "errors": errors,
            "status": "extracted",
        }

    async def match_candidates(self, state: AgentState) -> AgentState:
        """Node 3: ask Claude to rank candidates against the job description.
        This is the one node that is genuinely "the agent's job" — everything
        else is IO the MCP server now owns."""
        texts = state.get("extracted_texts", {})
        if not texts:
            return {"ranked_matches": [], "status": "no_candidates"}

        candidate_block = "\n\n".join(
            f"--- FILE: {path} ---\n{text[:3000]}" for path, text in texts.items()
        )
        prompt = f"""You are screening resumes against a job description.

JOB DESCRIPTION:
{state['job_description']}

CANDIDATES:
{candidate_block}

Return ONLY a JSON array, no prose, of objects:
[{{"file": "<path>", "score": <0-100 int>, "rationale": "<one sentence>"}}]
Order by score descending."""

        response = self.anthropic.messages.create(
            model=MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text_out = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        text_out = text_out.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            ranked = json.loads(text_out)
        except json.JSONDecodeError:
            return {"errors": [f"Could not parse ranking JSON: {text_out[:200]}"], "status": "failed"}
        return {"ranked_matches": ranked, "status": "matched"}

    async def save_results(self, state: AgentState) -> AgentState:
        """Node 4: write results back through the MCP server, not raw open()."""
        output_path = state.get("output_path", "match_results.json")
        payload = json.dumps(
            {
                "job_description": state["job_description"],
                "ranked_matches": state.get("ranked_matches", []),
                "errors": state.get("errors", []),
            },
            indent=2,
        )
        result = await self._call_tool(
            "write_output", file_path=output_path, content=payload, overwrite=True
        )
        if result.get("status") != "ok":
            return {"errors": [f"save_results failed: {result.get('error')}"], "status": "failed"}
        return {"status": "saved"}

    # -- routing -----------------------------------------------------
    @staticmethod
    def _route_after_discover(state: AgentState) -> str:
        if state.get("status") == "failed" or not state.get("resume_files"):
            return "end"
        return "extract"

    # -- graph assembly -----------------------------------------------------
    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("discover", self.discover_resumes)
        graph.add_node("extract", self.extract_content)
        graph.add_node("match", self.match_candidates)
        graph.add_node("save", self.save_results)

        graph.set_entry_point("discover")
        graph.add_conditional_edges(
            "discover", self._route_after_discover, {"extract": "extract", "end": END}
        )
        graph.add_edge("extract", "match")
        graph.add_edge("match", "save")
        graph.add_edge("save", END)
        return graph.compile()

    # -- public entrypoint -----------------------------------------------------
    async def run(self, directory: str, job_description: str, output_path: str) -> dict:
        await self._ensure_tools_loaded()
        initial_state: AgentState = {
            "directory": directory,
            "job_description": job_description,
            "output_path": output_path,
            "errors": [],
        }
        return await self.graph.ainvoke(initial_state)

    async def aclose(self):
        # MultiServerMCPClient manages its own subprocess lifecycles per call;
        # nothing to explicitly tear down beyond letting the process exit.
        pass


# ---------------------------------------------------------------------------
# Bonus: multi-MCP wiring example
# ---------------------------------------------------------------------------
def with_web_search_server(base_config: dict, web_search_command: list[str] | None = None) -> dict:
    """
    Demonstrates Part B.2 (bonus): plugging a second MCP server into the same
    agent so it can, e.g., pull a live job posting or company info before
    matching, without adding a single line of custom HTTP code to this file.

    If you have a web-search MCP server available (many public ones exist,
    e.g. exa, tavily, or brave-search MCP servers), point `web_search_command`
    at it, and its tools (e.g. `web_search`) will appear in
    `agent.tools_by_name` alongside the filesystem tools automatically.
    """
    if not web_search_command:
        return base_config
    config = dict(base_config)
    config["web_search"] = {
        "transport": "stdio",
        "command": web_search_command[0],
        "args": web_search_command[1:],
        "env": {**os.environ},
    }
    return config


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def _main_async(args: argparse.Namespace) -> None:
    job_description = args.job_text
    if args.job and os.path.exists(args.job):
        with open(args.job, encoding="utf-8") as f:
            job_description = f.read()

    config = build_mcp_config()
    agent = MatchingAgent(mcp_config=config)

    print(f"[agent] Discovering resumes in: {args.directory}")
    result = await agent.run(
        directory=args.directory,
        job_description=job_description,
        output_path=args.output,
    )

    print(f"[agent] Final status: {result.get('status')}")
    if result.get("errors"):
        print("[agent] Errors encountered:")
        for e in result["errors"]:
            print(f"  - {e}")

    for m in result.get("ranked_matches", []):
        print(f"  {m['score']:>3}  {m['file']}  — {m['rationale']}")

    print(f"[agent] Results written to: {args.output}")


def main():
    parser = argparse.ArgumentParser(description="MCP-powered resume matching agent")
    parser.add_argument("--directory", required=True, help="Directory of resumes (must be under MCP_ALLOWED_DIRS)")
    parser.add_argument("--job", help="Path to a job description text file")
    parser.add_argument("--job-text", default="", dest="job_text", help="Inline job description text")
    parser.add_argument("--output", default="match_results.json", help="Path to write ranked results")
    args = parser.parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
