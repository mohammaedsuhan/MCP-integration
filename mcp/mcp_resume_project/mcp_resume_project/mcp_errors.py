"""
mcp_errors.py
-------------
Structured error types for the MCP server.

The MCP protocol rides on JSON-RPC 2.0. When a tool call raises inside
FastMCP, the framework converts it into a JSON-RPC error object:

    {"jsonrpc": "2.0", "id": <id>, "error": {"code": <int>, "message": <str>}}

We define an application-level error taxonomy on top of the standard
JSON-RPC reserved codes (-32700..-32600) so clients (our LangGraph agent,
or any other MCP client) can branch on `error.code` instead of parsing
message strings.
"""

from __future__ import annotations


# Standard JSON-RPC 2.0 reserved range: -32768 to -32000
# We use the "server error" band (-32000 to -32099) reserved for
# implementation-defined server errors, per the JSON-RPC 2.0 spec.
class MCPErrorCode:
    PATH_NOT_ALLOWED = -32001
    FILE_NOT_FOUND = -32002
    FILE_TOO_LARGE = -32003
    UNSUPPORTED_FILE_TYPE = -32004
    EXTRACTION_FAILED = -32005
    WRITE_FAILED = -32006
    INVALID_ARGUMENT = -32602  # reuse JSON-RPC's own "Invalid params"
    INTERNAL_ERROR = -32000


class MCPToolError(Exception):
    """
    Base class for all tool-level errors raised by this server.
    Carries a JSON-RPC-flavored code + message so it serializes cleanly.
    """

    def __init__(self, code: int, message: str, data: dict | None = None):
        self.code = code
        self.message = message
        self.data = data or {}
        super().__init__(message)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "data": self.data}


class PathNotAllowedError(MCPToolError):
    def __init__(self, path: str, allowed: list[str]):
        super().__init__(
            MCPErrorCode.PATH_NOT_ALLOWED,
            f"Path '{path}' is outside the allowed directories.",
            {"path": path, "allowed_base_dirs": allowed},
        )


class FileNotFoundMCPError(MCPToolError):
    def __init__(self, path: str):
        super().__init__(
            MCPErrorCode.FILE_NOT_FOUND, f"File not found: '{path}'", {"path": path}
        )


class FileTooLargeError(MCPToolError):
    def __init__(self, path: str, size_mb: float, limit_mb: int):
        super().__init__(
            MCPErrorCode.FILE_TOO_LARGE,
            f"File '{path}' is {size_mb:.1f}MB, exceeds limit of {limit_mb}MB.",
            {"path": path, "size_mb": size_mb, "limit_mb": limit_mb},
        )


class UnsupportedFileTypeError(MCPToolError):
    def __init__(self, path: str, supported: tuple[str, ...]):
        super().__init__(
            MCPErrorCode.UNSUPPORTED_FILE_TYPE,
            f"Unsupported file type for '{path}'. Supported: {supported}",
            {"path": path, "supported_extensions": list(supported)},
        )


class ExtractionFailedError(MCPToolError):
    def __init__(self, path: str, reason: str):
        super().__init__(
            MCPErrorCode.EXTRACTION_FAILED,
            f"Failed to extract text from '{path}': {reason}",
            {"path": path, "reason": reason},
        )
