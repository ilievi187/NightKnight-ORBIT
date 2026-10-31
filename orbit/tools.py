"""Small, auditable tool registry. Explicit human approval required for all writes."""
import os
import tempfile
from pathlib import Path

MAX_READ_BYTES = 32_768
MAX_WRITE_CHARS = 60_000
DENIED_NAMES = {".env", ".ssh", ".aws", ".gnupg", ".git", ".npmrc", ".netrc", ".pypirc", "credentials.json", "secrets.json"}
DENIED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files", "description": "List files within the configured project workspace. Read-only.",
            "parameters": {"type":"object", "properties":{"path":{"type":"string","description":"Relative subdirectory, or . for workspace root"}}, "required":["path"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file", "description": "Read up to 32768 bytes of UTF-8 from a file within the project workspace. Read-only.",
            "parameters": {"type":"object", "properties":{"path":{"type":"string","description":"Path relative to workspace root"}}, "required":["path"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file", "description": "Create or replace a text file in the workspace. REQUIRES human approval before any modification.",
            "parameters": {"type":"object", "properties":{"path":{"type":"string"},"content":{"type":"string"}}, "required":["path","content"]}
        }
    },
]

WRITE_TOOLS = {"write_file"}


def checked_path(root: Path, supplied: str) -> Path:
    if not isinstance(supplied, str) or not supplied or "\x00" in supplied:
        raise ValueError("Invalid path")
    rel = Path(supplied)
    if rel.is_absolute():
        raise PermissionError("Absolute paths are not allowed")
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise PermissionError("Path escapes workspace")
    if any(p in DENIED_NAMES or p.startswith(".env") or p.startswith("id_rsa") or p.startswith("id_ed25519") for p in (*rel.parts, *candidate.relative_to(root.resolve()).parts)):
        raise PermissionError("Sensitive path is restricted")
    if candidate.suffix.lower() in DENIED_SUFFIXES:
        raise PermissionError("Sensitive file extension is restricted")
    return candidate


def execute_tool(name: str, args: dict, workspace: Path, *, approved: bool = False) -> str:
    if name not in {"list_files", "read_file", "write_file"}:
        raise ValueError(f"Unknown tool: {name}")
    if not isinstance(args, dict):
        raise ValueError("Tool arguments must be a JSON object")
    target = checked_path(workspace, args.get("path"))
    if name == "list_files":
        if not target.is_dir():
            raise ValueError("Not a directory")
        results = []
        for item in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            try:
                rel = str(item.relative_to(workspace))
                checked_path(workspace, rel)
                if item.name in DENIED_NAMES:
                    continue
                results.append({"name": item.name, "type": "directory" if item.is_dir() else "file", "size": item.stat().st_size if item.is_file() else 0})
                if len(results) >= 75:
                    break
            except (PermissionError, ValueError, OSError):
                continue
        import json
        return json.dumps(results, ensure_ascii=False)
    if name == "read_file":
        if not target.is_file():
            raise ValueError("Not a file")
        with target.open("rb") as f:
            data = f.read(MAX_READ_BYTES + 1)
        if b"\x00" in data:
            raise ValueError("Binary file not supported")
        truncated = len(data) > MAX_READ_BYTES
        decoded = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
        return decoded + ("\n[TRUNCATED at 32KiB]" if truncated else "")
    if not approved:
        raise PermissionError("Write requires explicit human approval")
    if target == workspace:
        raise ValueError("Cannot overwrite workspace root")
    content = args.get("content")
    if not isinstance(content, str) or len(content) > MAX_WRITE_CHARS:
        raise ValueError("Text content is required and must be at most 60,000 characters")
    if target.exists() and not target.is_file():
        raise ValueError("Target is not a regular file")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace; new files are private until user explicitly changes permissions.
    fd, temp_name = tempfile.mkstemp(prefix=".orbit-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return f"Wrote {len(content)} characters to {target.relative_to(workspace)}"
