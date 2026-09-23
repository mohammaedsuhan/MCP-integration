"""
config.py
---------
Central configuration for the Filesystem MCP Server.

Design notes:
- All configurable values can be overridden via environment variables so the
  server can be deployed the same way in dev / staging / prod (12-factor style).
- ALLOWED_BASE_DIRS enforces a security boundary: every filesystem tool must
  resolve its path and confirm it lives under one of these directories before
  touching disk. This prevents path-traversal ("../../etc/passwd") attacks
  from an MCP client (trusted or not).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return default
    return [p.strip() for p in raw.split(os.pathsep) if p.strip()]


@dataclass
class ServerConfig:
    # Security boundary: tools may only read/write inside these directories.
    allowed_base_dirs: list[str] = field(
        default_factory=lambda: _env_list(
            "MCP_ALLOWED_DIRS", [str(Path.cwd() / "resumes")]
        )
    )

    # File types the server knows how to extract text from.
    supported_extensions: tuple[str, ...] = (".txt", ".pdf", ".docx", ".md")

    # Guard rails.
    max_file_size_mb: int = int(os.environ.get("MCP_MAX_FILE_SIZE_MB", "20"))
    max_batch_workers: int = int(os.environ.get("MCP_MAX_BATCH_WORKERS", "4"))

    # Where watch_directory() persists its "last seen" state between calls,
    # since MCP tools are stateless request/response and the process may be
    # restarted between polls.
    watch_state_path: str = os.environ.get(
        "MCP_WATCH_STATE_PATH", str(Path.cwd() / ".mcp_watch_state.json")
    )

    server_name: str = "filesystem-resume-server"
    server_version: str = "1.0.0"

    def resolve_and_validate(self, path_str: str) -> Path:
        """
        Resolve a user/agent-supplied path and ensure it falls under an
        allowed base directory. Raises PermissionError otherwise.
        This is the single choke point every tool routes through.
        """
        candidate = Path(path_str).expanduser().resolve()
        for base in self.allowed_base_dirs:
            base_resolved = Path(base).expanduser().resolve()
            try:
                candidate.relative_to(base_resolved)
                return candidate
            except ValueError:
                continue
        raise PermissionError(
            f"Path '{path_str}' is outside allowed directories: "
            f"{self.allowed_base_dirs}"
        )

    def as_dict(self) -> dict:
        return {
            "server_name": self.server_name,
            "server_version": self.server_version,
            "allowed_base_dirs": self.allowed_base_dirs,
            "supported_extensions": list(self.supported_extensions),
            "max_file_size_mb": self.max_file_size_mb,
            "max_batch_workers": self.max_batch_workers,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2)


CONFIG = ServerConfig()
