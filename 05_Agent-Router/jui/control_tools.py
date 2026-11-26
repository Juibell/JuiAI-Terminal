import asyncio
import difflib
import hashlib
import json
import shutil
import time
import uuid

from . import plugins
from .config import child_environment
from .control_models import cloud_guard


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


class ToolBroker:
    def __init__(self, control):
        self.control = control
        self.pending = {}

    def context(self, agent):
        stage = self.control.active_stage
        if (
            not stage
            or stage["agent"] != agent
            or stage["run_id"] not in self.control.workers
        ):
            raise ValueError("No active task authorizes this agent")
        run = self.control.store.get("run", stage["run_id"])
        if run["status"] in {"cancelled", "interrupted", "failed"}:
            raise ValueError("Task authorization was revoked")
        return stage, run

    async def approval(self, agent, action, arguments, preview):
        stage, run = self.context(agent)
        identity = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        item = {
            "id": identity,
            "run_id": run["id"],
            "workspace": run["workspace"],
            "workspace_id": run.get("workspace_id", "default"),
            "agent": agent,
            "stage_nonce": stage["nonce"],
            "action": action,
            "arguments": arguments,
            "preview": preview,
            "status": "pending",
            "expires": time.time() + 120,
        }
        self.control.store.put("approval", item)
        self.pending[identity] = future
        self.control.emit("approval", approval=item, run_id=run["id"])
        try:
            accepted = await asyncio.wait_for(future, 120)
            current, _ = self.context(agent)
            if current["nonce"] != item["stage_nonce"] or not accepted:
                raise ValueError("Operation was not approved")
            category = (
                "workspace"
                if action == "write_file"
                else "commands"
                if action == "run_command"
                else "desktop"
            )
            if not self.control.store.get("preferences", "tools").get(category):
                self.control.store.put(
                    "approval",
                    {
                        **self.control.store.get("approval", identity),
                        "status": "revoked",
                    },
                )
                raise ValueError(
                    "This tool category was disabled after approval was requested"
                )
            return item
        finally:
            self.pending.pop(identity, None)
            latest = self.control.store.get("approval", identity)
            if latest["status"] == "pending":
                self.control.store.put("approval", {**latest, "status": "expired"})
                self.control.emit("approval_changed", id=identity)

    def decide(self, identity, accept):
        item = self.control.store.get("approval", identity)
        future = self.pending.get(identity)
        stage = self.control.active_stage
        if (
            item["status"] != "pending"
            or item["expires"] <= time.time()
            or not future
            or future.done()
            or not stage
            or stage["nonce"] != item["stage_nonce"]
        ):
            raise ValueError("Approval has expired or was already decided")
        self.control.store.put(
            "approval", {**item, "status": "approved" if accept else "rejected"}
        )
        future.set_result(accept)
        self.control.emit("approval_changed", id=identity)
        return {"accepted": accept}

    def revoke(self, run_id):
        for item in self.control.store.list("approval"):
            if item["run_id"] == run_id and item["status"] == "pending":
                self.control.store.put("approval", {**item, "status": "cancelled"})
                future = self.pending.get(item["id"])
                if future and not future.done():
                    future.set_result(False)

    async def call(self, agent, action, arguments):
        _stage, run = self.context(agent)
        root = run["workspace"]
        settings = self.control.store.get("preferences", "tools")
        category = (
            "desktop"
            if action
            in {"inspect_browser", "open_google", "search_news", "open_project_folder"}
            else "git"
            if action == "git_status"
            else "commands"
            if action == "run_command"
            else "workspace"
        )
        if not settings.get(category, False):
            raise ValueError("Tool category is disabled")
        profile = run["profiles"][agent]
        cloud_guard(profile, True, run["policy"])
        self.control.change_run(run["id"], sensitive=True)
        if action == "list_files":
            return plugins.list_files(arguments.get("path", "."), root)
        if action == "read_file":
            return plugins.read_file(arguments["path"], root)
        if action == "git_status":
            return await asyncio.to_thread(plugins.git_status, root)
        item = None
        try:
            if action == "write_file":
                path = plugins.workspace_path(arguments["path"], root)
                text = arguments["content"]
                if (
                    len(text.encode()) > 128_000
                    or path.is_symlink()
                    or not path.parent.is_dir()
                    or (path.exists() and path.stat().st_size > 128_000)
                ):
                    raise ValueError(
                        "Writes require an existing parent and at most 128 KB of UTF-8 text"
                    )
                before = path.read_text(encoding="utf-8") if path.exists() else ""
                digest = fingerprint(path)
                preview = "".join(
                    difflib.unified_diff(
                        before.splitlines(True),
                        text.splitlines(True),
                        fromfile=arguments["path"],
                        tofile=arguments["path"],
                    )
                )
                item = await self.approval(agent, action, arguments, preview[:160000])
                checked = plugins.workspace_path(arguments["path"], root)
                if checked != path or fingerprint(checked) != digest:
                    raise ValueError(
                        "File changed after approval; request a new approval"
                    )
                path.write_text(text, encoding="utf-8")
                result = {
                    "success": True,
                    "path": arguments["path"],
                    "sha256": fingerprint(path),
                }
            elif action == "run_command":
                argv = arguments.get("argv", [])
                if (
                    not isinstance(argv, list)
                    or not 1 <= len(argv) <= 40
                    or any(
                        not isinstance(a, str) or len(a) > 8000 or "\x00" in a
                        for a in argv
                    )
                ):
                    raise ValueError("Expected a bounded executable/argument list")
                executable = shutil.which(argv[0])
                if not executable:
                    raise ValueError("Executable not found")
                timeout = min(max(int(arguments.get("timeout", 30)), 1), 120)
                item = await self.approval(
                    agent,
                    action,
                    arguments,
                    json.dumps(
                        {
                            "executable": executable,
                            "argv": argv[1:],
                            "cwd": root,
                            "timeout": timeout,
                        },
                        ensure_ascii=False,
                    ),
                )
                self.context(agent)
                from .command_worker import execute

                result = await execute(
                    [executable, *argv[1:]], root, child_environment(), timeout
                )
            elif action in {
                "inspect_browser",
                "open_google",
                "search_news",
                "open_project_folder",
            }:
                if action != "inspect_browser":
                    item = await self.approval(
                        agent,
                        action,
                        arguments,
                        json.dumps(arguments, ensure_ascii=False),
                    )
                # Bypass only the legacy coarse grant check, never this broker's approval.
                result = await plugins.desktop_serial(action, arguments, approved=True)
                result.pop("image_png", None)
            else:
                raise ValueError("Unsupported tool")
            if item:
                self.control.store.put(
                    "approval",
                    {
                        **self.control.store.get("approval", item["id"]),
                        "status": "executed",
                    },
                )
            self.control.emit(
                "tool_result",
                run_id=run["id"],
                agent=agent,
                tool=action,
                success=result.get("success", False),
            )
            return result
        except BaseException:
            if item:
                self.control.store.put(
                    "approval",
                    {
                        **self.control.store.get("approval", item["id"]),
                        "status": "uncertain",
                    },
                )
            raise
