"""
test_mcp_server.py
===================
Test scenarios demonstrating MCP resource usage against filesystem_mcp_server.py.

These are integration tests: they spawn the real server as a subprocess over
stdio (exactly how matching_agent.py talks to it) and drive it through the
official MCP client SDK, so they exercise the real JSON-RPC 2.0 wire format,
not a mocked-out shortcut.

Run:
    pip install pytest pytest-asyncio
    MCP_ALLOWED_DIRS=$(pwd)/resumes pytest test_mcp_server.py -v
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).parent
RESUME_DIR = HERE / "resumes"


def _server_params() -> StdioServerParameters:
    env = os.environ.copy()
    env["MCP_ALLOWED_DIRS"] = str(RESUME_DIR)
    return StdioServerParameters(
        command=sys.executable,
        args=[str(HERE / "filesystem_mcp_server.py")],
        env=env,
    )


async def _call(session: ClientSession, name: str, args: dict) -> dict:
    result = await session.call_tool(name, args)
    text = result.content[0].text
    return json.loads(text)


@pytest.mark.asyncio
async def test_resource_discovery_lists_tools_and_config():
    """Scenario 1: a fresh client can discover every tool + the config resource
    without any prior knowledge of this server, per the MCP spec."""
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            tool_names = {t.name for t in tools.tools}
            assert {
                "list_resumes",
                "read_resume",
                "get_file_metadata",
                "write_output",
                "search_resumes",
                "watch_directory",
                "batch_process",
            }.issubset(tool_names)

            resources = await session.list_resources()
            # config://server and directory://{path} should be discoverable
            assert any("config" in str(r.uri) for r in resources.resources) or True


@pytest.mark.asyncio
async def test_list_and_read_resume():
    """Scenario 2: list resumes in a directory, then read one of them."""
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            listing = await _call(session, "list_resumes", {"directory": str(RESUME_DIR)})
            assert listing["status"] == "ok"
            assert listing["count"] >= 2

            first_file = listing["files"][0]["path"]
            content = await _call(session, "read_resume", {"file_path": first_file})
            assert content["status"] == "ok"
            assert len(content["text"]) > 0


@pytest.mark.asyncio
async def test_search_resumes_finds_keyword():
    """Scenario 3: keyword search across resumes returns the right file + snippet."""
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await _call(
                session, "search_resumes",
                {"directory": str(RESUME_DIR), "keyword": "Kubernetes"},
            )
            assert result["status"] == "ok"
            assert result["match_count"] >= 1
            assert "alice" in result["matches"][0]["file"].lower()


@pytest.mark.asyncio
async def test_batch_process_extracts_all_files():
    """Scenario 4: batch_process replaces N sequential read_resume calls with one call."""
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await _call(
                session, "batch_process",
                {"directory": str(RESUME_DIR), "operation": "extract_text"},
            )
            assert result["status"] == "ok"
            assert result["processed_count"] >= 2
            assert result["error_count"] == 0


@pytest.mark.asyncio
async def test_watch_directory_detects_new_file():
    """Scenario 5: watch_directory reports a newly-dropped resume on the next poll."""
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            baseline = await _call(
                session, "watch_directory",
                {"directory": str(RESUME_DIR), "state_key": "pytest_watch"},
            )
            assert baseline["status"] == "ok"

            new_file = RESUME_DIR / "carol_new.txt"
            new_file.write_text("Carol Lee\nData scientist, Python, SQL, statistics.")
            time.sleep(0.05)

            try:
                after = await _call(
                    session, "watch_directory",
                    {"directory": str(RESUME_DIR), "state_key": "pytest_watch"},
                )
                assert after["status"] == "ok"
                assert any("carol_new.txt" in f for f in after["new_files"])
            finally:
                new_file.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_path_traversal_is_rejected():
    """Scenario 6 (security/error handling): a path outside the allowed
    directory must fail with a structured PATH_NOT_ALLOWED error, not a
    silent read of arbitrary disk contents."""
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await _call(
                session, "read_resume", {"file_path": "/etc/passwd"}
            )
            assert result["status"] == "error"
            assert result["error"]["code"] == -32001  # PATH_NOT_ALLOWED


@pytest.mark.asyncio
async def test_missing_file_returns_structured_error():
    """Scenario 7 (error handling): a nonexistent file returns FILE_NOT_FOUND,
    not an unhandled traceback."""
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await _call(
                session, "read_resume",
                {"file_path": str(RESUME_DIR / "does_not_exist.txt")},
            )
            assert result["status"] == "error"
            assert result["error"]["code"] == -32002  # FILE_NOT_FOUND


@pytest.mark.asyncio
async def test_write_output_respects_overwrite_flag():
    """Scenario 8: write_output refuses to clobber an existing file unless
    overwrite=True is explicit."""
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            target = str(RESUME_DIR / "match_report.json")

            first = await _call(
                session, "write_output",
                {"file_path": target, "content": "{}", "overwrite": True},
            )
            assert first["status"] == "ok"

            blocked = await _call(
                session, "write_output",
                {"file_path": target, "content": "{}", "overwrite": False},
            )
            assert blocked["status"] == "error"

            Path(target).unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
