"""
filesystem_mcp_server.py
=========================
An MCP (Model Context Protocol) server that exposes filesystem operations
needed by a resume-matching agent, replacing the ad-hoc "custom tool"
functions from Milestone 1 with a standardized, discoverable, JSON-RPC 2.0
compliant interface.

Why MCP instead of hand-rolled tools?
- Any MCP-compliant client (our LangGraph agent, Claude Desktop, another
  team's agent) can discover and call these tools without bespoke glue code.
- Transport (stdio here, but the same code works over SSE/HTTP) and message
  framing (JSON-RPC 2.0) are handled by the `mcp` SDK, not us.
- Errors, schemas, and capability negotiation follow one spec instead of
  whatever convention Milestone 1's functions happened to use.

Run it directly for local testing:
    python filesystem_mcp_server.py

Or let a client spawn it over stdio (see matching_agent.py).

Install:
    pip install "mcp[cli]" pypdf python-docx
"""

from __future__ import annotations

import fnmatch
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from config import CONFIG
from mcp_errors import (
    ExtractionFailedError,
    FileNotFoundMCPError,
    FileTooLargeError,
    MCPToolError,
    PathNotAllowedError,
    UnsupportedFileTypeError,
)

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------
mcp = FastMCP(CONFIG.server_name)


# ---------------------------------------------------------------------------
# Internal helpers (not exposed as MCP tools themselves)
# ---------------------------------------------------------------------------
def _safe_path(path_str: str) -> Path:
    try:
        return CONFIG.resolve_and_validate(path_str)
    except PermissionError:
        raise PathNotAllowedError(path_str, CONFIG.allowed_base_dirs)


def _check_size(path: Path) -> None:
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > CONFIG.max_file_size_mb:
        raise FileTooLargeError(str(path), size_mb, CONFIG.max_file_size_mb)


def _extract_text(path: Path) -> str:
    """Extract plain text from .txt/.md/.pdf/.docx files."""
    suffix = path.suffix.lower()
    if suffix not in CONFIG.supported_extensions:
        raise UnsupportedFileTypeError(str(path), CONFIG.supported_extensions)

    try:
        if suffix in (".txt", ".md"):
            return path.read_text(encoding="utf-8", errors="replace")

        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError as e:
                raise ExtractionFailedError(
                    str(path), "pypdf not installed (pip install pypdf)"
                ) from e
            reader = PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages)

        if suffix == ".docx":
            try:
                import docx  # python-docx
            except ImportError as e:
                raise ExtractionFailedError(
                    str(path), "python-docx not installed (pip install python-docx)"
                ) from e
            document = docx.Document(str(path))
            return "\n".join(p.text for p in document.paragraphs)

    except MCPToolError:
        raise
    except Exception as e:  # noqa: BLE001 - convert any parser failure to our type
        raise ExtractionFailedError(str(path), str(e)) from e

    raise UnsupportedFileTypeError(str(path), CONFIG.supported_extensions)


def _file_metadata(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "name": path.name,
        "extension": path.suffix.lower(),
        "size_bytes": stat.st_size,
        "modified_time": stat.st_mtime,
        "modified_iso": time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime)
        ),
    }


def _iter_matching_files(directory: Path, pattern: str) -> list[Path]:
    return sorted(
        p
        for p in directory.rglob("*")
        if p.is_file()
        and fnmatch.fnmatch(p.name, pattern)
        and p.suffix.lower() in CONFIG.supported_extensions
    )


# ---------------------------------------------------------------------------
# MCP Tools (Milestone 1 parity)
# ---------------------------------------------------------------------------
@mcp.tool()
def list_resumes(directory: str) -> dict:
    """
    List all supported resume files in a directory (recursive).

    Args:
        directory: Path to search, must be under an allowed base directory.

    Returns:
        dict with `count` and `files` (each file's metadata).
    """
    try:
        dir_path = _safe_path(directory)
        if not dir_path.is_dir():
            raise FileNotFoundMCPError(directory)
        files = _iter_matching_files(dir_path, "*")
        return {
            "status": "ok",
            "count": len(files),
            "files": [_file_metadata(f) for f in files],
        }
    except MCPToolError as e:
        return {"status": "error", "error": e.to_dict()}


@mcp.tool()
def read_resume(file_path: str) -> dict:
    """
    Read and extract text content from a single resume file
    (.txt, .md, .pdf, .docx).

    Args:
        file_path: Path to the resume file.

    Returns:
        dict with extracted `text` and file `metadata`.
    """
    try:
        path = _safe_path(file_path)
        if not path.is_file():
            raise FileNotFoundMCPError(file_path)
        _check_size(path)
        text = _extract_text(path)
        return {"status": "ok", "text": text, "metadata": _file_metadata(path)}
    except MCPToolError as e:
        return {"status": "error", "error": e.to_dict()}


@mcp.tool()
def get_file_metadata(file_path: str) -> dict:
    """Return size, timestamps, and type info for a single file without reading its content."""
    try:
        path = _safe_path(file_path)
        if not path.is_file():
            raise FileNotFoundMCPError(file_path)
        return {"status": "ok", "metadata": _file_metadata(path)}
    except MCPToolError as e:
        return {"status": "error", "error": e.to_dict()}


@mcp.tool()
def write_output(file_path: str, content: str, overwrite: bool = False) -> dict:
    """
    Write matching results / reports to disk under an allowed directory.

    Args:
        file_path: Destination path.
        content: Text content to write.
        overwrite: If False and the file exists, the call fails safely.
    """
    try:
        path = _safe_path(file_path)
        if path.exists() and not overwrite:
            return {
                "status": "error",
                "error": {
                    "code": -32006,
                    "message": f"File '{file_path}' already exists; pass overwrite=True to replace it.",
                },
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"status": "ok", "metadata": _file_metadata(path)}
    except MCPToolError as e:
        return {"status": "error", "error": e.to_dict()}
    except OSError as e:
        return {
            "status": "error",
            "error": {"code": -32006, "message": f"Write failed: {e}"},
        }


@mcp.tool()
def search_resumes(directory: str, keyword: str, case_sensitive: bool = False) -> dict:
    """
    Search resume file contents for a keyword (e.g. a skill or job title).

    Args:
        directory: Directory to search recursively.
        keyword: Term to look for.
        case_sensitive: Whether matching is case-sensitive.

    Returns:
        dict listing files that contain the keyword, with a short snippet.
    """
    try:
        dir_path = _safe_path(directory)
        if not dir_path.is_dir():
            raise FileNotFoundMCPError(directory)

        matches = []
        needle = keyword if case_sensitive else keyword.lower()
        for f in _iter_matching_files(dir_path, "*"):
            try:
                text = _extract_text(f)
            except MCPToolError:
                continue  # skip unreadable files, don't fail the whole search
            haystack = text if case_sensitive else text.lower()
            idx = haystack.find(needle)
            if idx != -1:
                start = max(0, idx - 40)
                end = min(len(text), idx + len(keyword) + 40)
                matches.append(
                    {"file": str(f), "snippet": text[start:end].strip()}
                )
        return {"status": "ok", "keyword": keyword, "match_count": len(matches), "matches": matches}
    except MCPToolError as e:
        return {"status": "error", "error": e.to_dict()}


# ---------------------------------------------------------------------------
# New MCP-specific capabilities
# ---------------------------------------------------------------------------
def _load_watch_state() -> dict:
    p = Path(CONFIG.watch_state_path)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_watch_state(state: dict) -> None:
    Path(CONFIG.watch_state_path).write_text(json.dumps(state, indent=2))


@mcp.tool()
def watch_directory(directory: str, state_key: str = "default") -> dict:
    """
    Detect resumes that are new, modified, or removed since the last call
    with the same `state_key`. Designed to be polled (e.g. every N seconds
    by the agent or a scheduler) since MCP tool calls are stateless
    request/response, not long-lived subscriptions.

    Args:
        directory: Directory to monitor.
        state_key: Identifier so multiple watchers can track independent
                   directories/sessions without clobbering each other's state.

    Returns:
        dict with `new_files`, `modified_files`, `removed_files`.
    """
    try:
        dir_path = _safe_path(directory)
        if not dir_path.is_dir():
            raise FileNotFoundMCPError(directory)

        current = {
            str(f): f.stat().st_mtime for f in _iter_matching_files(dir_path, "*")
        }

        all_state = _load_watch_state()
        previous = all_state.get(state_key, {})

        new_files = [f for f in current if f not in previous]
        modified_files = [
            f for f in current if f in previous and current[f] > previous[f] + 1e-6
        ]
        removed_files = [f for f in previous if f not in current]

        all_state[state_key] = current
        _save_watch_state(all_state)

        return {
            "status": "ok",
            "directory": str(dir_path),
            "checked_at": time.time(),
            "new_files": new_files,
            "modified_files": modified_files,
            "removed_files": removed_files,
        }
    except MCPToolError as e:
        return {"status": "error", "error": e.to_dict()}


@mcp.tool()
def batch_process(
    directory: str,
    operation: str = "extract_text",
    file_pattern: str = "*",
    max_workers: int | None = None,
) -> dict:
    """
    Process every matching file in a directory concurrently. This is the
    MCP-native replacement for looping one-file-at-a-time tool calls from
    the agent, cutting round trips from O(n) to O(1).

    Args:
        directory: Directory to process.
        operation: "extract_text" or "metadata".
        file_pattern: fnmatch-style filter, e.g. "*.pdf".
        max_workers: Thread pool size (defaults to server config).

    Returns:
        dict with per-file `results` and any `errors`, keyed by path.
    """
    try:
        dir_path = _safe_path(directory)
        if not dir_path.is_dir():
            raise FileNotFoundMCPError(directory)

        if operation not in ("extract_text", "metadata"):
            return {
                "status": "error",
                "error": {
                    "code": -32602,
                    "message": f"Unknown operation '{operation}'. Use 'extract_text' or 'metadata'.",
                },
            }

        files = _iter_matching_files(dir_path, file_pattern)
        workers = max_workers or CONFIG.max_batch_workers

        results: dict[str, Any] = {}
        errors: dict[str, Any] = {}

        def _process_one(f: Path):
            if operation == "extract_text":
                _check_size(f)
                return str(f), _extract_text(f)
            return str(f), _file_metadata(f)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {pool.submit(_process_one, f): f for f in files}
            for future in as_completed(future_map):
                f = future_map[future]
                try:
                    key, value = future.result()
                    results[key] = value
                except MCPToolError as e:
                    errors[str(f)] = e.to_dict()
                except Exception as e:  # noqa: BLE001
                    errors[str(f)] = {"code": -32000, "message": str(e)}

        return {
            "status": "ok",
            "operation": operation,
            "processed_count": len(results),
            "error_count": len(errors),
            "results": results,
            "errors": errors,
        }
    except MCPToolError as e:
        return {"status": "error", "error": e.to_dict()}


# ---------------------------------------------------------------------------
# Resource discovery endpoints
# (MCP resources are how clients can *browse* what's available without
#  invoking a tool call — useful for a UI or an agent building context.)
# ---------------------------------------------------------------------------
@mcp.resource("config://server")
def server_config_resource() -> str:
    """Server configuration: allowed directories, limits, supported types."""
    return CONFIG.to_json()


@mcp.resource("directory://{path}")
def directory_listing_resource(path: str) -> str:
    """Browse a directory's resume files as a discoverable resource."""
    try:
        dir_path = _safe_path(path)
        if not dir_path.is_dir():
            return json.dumps({"status": "error", "message": f"Not a directory: {path}"})
        files = _iter_matching_files(dir_path, "*")
        return json.dumps(
            {"status": "ok", "directory": str(dir_path), "files": [f.name for f in files]},
            indent=2,
        )
    except MCPToolError as e:
        return json.dumps({"status": "error", "error": e.to_dict()})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Ensure the default allowed directory exists so a fresh checkout runs.
    for base in CONFIG.allowed_base_dirs:
        os.makedirs(base, exist_ok=True)
    # stdio transport: the client (matching_agent.py) spawns this process
    # and speaks JSON-RPC 2.0 over stdin/stdout, per the MCP spec.
    mcp.run(transport="stdio")
