import asyncio
import hmac
import json
import os
import subprocess
import time
from pathlib import Path

from .config import ROOT, STATE, WORKSPACE, load_settings

DENIED = {
    ".git",
    ".env",
    ".runtime",
    ".venv",
    "node_modules",
    "__pycache__",
    ".ssh",
    ".aws",
    ".azure",
    ".gnupg",
    ".codex",
    "13_backups",
    ".openclaw",
    ".hermes",
    ".opencode",
    ".config",
    ".local",
}
desktop_lock = asyncio.Lock()


def workspace_root():
    context = STATE / "task-context.json"
    if context.exists():
        value = json.loads(context.read_text(encoding="utf-8"))
        if value.get("expires", 0) <= time.time():
            raise ValueError("Workspace grant expired")
        return Path(value["workspace"])
    if os.environ.get("JUI_AGENT"):
        raise ValueError("No active workspace task")
    return WORKSPACE


def workspace_path(relative: str = ".", root=None) -> Path:
    root = Path(root) if root is not None else workspace_root()
    if "\\" in relative:
        relative = relative.replace("\\", "/")
    candidate = Path(relative)
    if candidate.is_absolute() or ":" in relative:
        raise ValueError("Only workspace-relative paths are allowed")
    path = (root / candidate).resolve()
    path.relative_to(root.resolve())
    if any(
        part.lower() in DENIED or part.lower().startswith(".env")
        for part in path.relative_to(root.resolve()).parts
    ):
        raise ValueError("Private or runtime path is not accessible")
    if any(part.lower() in DENIED for part in path.parts):
        raise ValueError("Private or runtime path is not accessible")
    if path.name.lower() in {
        "secrets.json",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
    }:
        raise ValueError("Credential files are not accessible")
    return path


def enabled(name):
    if not load_settings()["plugins"].get(name):
        raise ValueError("Plugin is disabled")


def list_files(relative=".", root=None):
    enabled("workspace")
    root = Path(root) if root is not None else workspace_root()
    directory = workspace_path(relative, root)
    items = []
    for path in sorted(
        directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
    ):
        try:
            safe = workspace_path(str(path.relative_to(root)), root)
        except (ValueError, OSError):
            continue
        items.append(
            {
                "path": str(safe.relative_to(root)).replace("\\", "/"),
                "directory": safe.is_dir(),
                "bytes": safe.stat().st_size if safe.is_file() else None,
            }
        )
        if len(items) == 200:
            break
    return {"success": True, "items": items, "untrusted_data": True}


def read_file(relative, root=None):
    enabled("workspace")
    path = workspace_path(relative, root)
    if not path.is_file() or path.stat().st_size > 128_000:
        raise ValueError("Only UTF-8 text files up to 128 KB are supported")
    return {
        "success": True,
        "path": relative,
        "text": path.read_text(encoding="utf-8-sig"),
        "untrusted_data": True,
    }


def git_status(root=None):
    enabled("git")
    root = Path(root) if root is not None else workspace_root()
    # Never walk upward into an unrelated parent repository.
    if not (root / ".git").exists():
        return {"success": True, "repository": False, "status": []}
    result = subprocess.run(
        [
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
        ],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return {
        "success": result.returncode == 0,
        "repository": True,
        "status": result.stdout[:8000].splitlines(),
    }


def desktop_allowed(token):
    try:
        grant = json.loads((STATE / "desktop-grant.json").read_text(encoding="utf-8"))
        expected = grant.get("token", "")
        return (
            bool(token and expected)
            and hmac.compare_digest(expected, token)
            and grant.get("expires", 0) > time.time()
        )
    except (OSError, ValueError, TypeError):
        return False


async def desktop(action, arguments=None, token=""):
    async with desktop_lock:
        return await desktop_serial(action, arguments, token)


async def desktop_serial(action, arguments=None, token="", *, approved=False):
    enabled("desktop")
    if action != "inspect_browser" and not approved and not desktop_allowed(token):
        return {
            "success": False,
            "status": "blocked",
            "code": "desktop_permission_required",
        }
    if action not in {
        "inspect_browser",
        "open_google",
        "search_news",
        "open_project_folder",
    }:
        raise ValueError("Unsupported desktop action")
    tool_root = ROOT / "06_MCP-Tools/Desktop"
    if (
        not (tool_root / ".venv/Scripts/python.exe").is_file()
        or not (tool_root / "desktop.py").is_file()
    ):
        raise ValueError("The optional desktop worker is not installed.")
    probe = await asyncio.create_subprocess_exec(
        str(tool_root / ".venv/Scripts/python.exe"),
        "-c",
        "import sys; print(sys._base_executable)",
        stdout=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    base, _ = await probe.communicate()
    bootstrap = "import site,runpy,sys; site.addsitedir(sys.argv[1]); sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')"
    worker = await asyncio.create_subprocess_exec(
        base.decode().strip(),
        "-c",
        bootstrap,
        str(tool_root / ".venv/Lib/site-packages"),
        str(tool_root / "desktop.py"),
        "--request",
        json.dumps({"action": action, "arguments": arguments or {}}),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        output, _ = await asyncio.wait_for(worker.communicate(), 65)
        if worker.returncode:
            raise RuntimeError("Desktop worker failed")
        return json.loads(output)
    except BaseException:
        if worker.returncode is None:
            worker.kill()
            await worker.communicate()
        raise
