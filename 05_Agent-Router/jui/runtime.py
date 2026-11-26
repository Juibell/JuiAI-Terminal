import asyncio
import os
import subprocess
from pathlib import Path

from .config import (
    ROOT,
    STATE,
    WORKSPACE,
    child_environment,
    load_secrets,
    load_settings,
)
from .logs import bounded_log


class Runtime:
    """Own only this workbench's isolated children, never a standalone gateway."""

    def __init__(self):
        self.children = []
        self.logs = []
        self.job = None

    async def start(self, names=None, state=None, model_base=None, model_key=None):
        state = state or STATE
        settings = load_settings()
        secret = load_secrets()
        if model_base:
            settings["model"]["base_url"] = model_base
        if model_key:
            secret["model_key"] = model_key
        logs = ROOT / "12_Logs/Workbench"
        logs.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            import win32job

            self.job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(
                self.job, win32job.JobObjectExtendedLimitInformation
            )
            info["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                self.job, win32job.JobObjectExtendedLimitInformation, info
            )
        exe = settings["executables"]
        env = child_environment()
        env.update(JUI_MODEL_KEY=secret["model_key"], JUI_LOG_DIR=str(logs))
        (state / "openclaw/windows-home").mkdir(parents=True, exist_ok=True)
        launches = {
            "openclaw": (
                [
                    exe["node"],
                    exe["openclaw"],
                    "gateway",
                    "run",
                    "--port",
                    "18790",
                    "--bind",
                    "loopback",
                    "--ws-log",
                    "compact",
                ],
                {
                    "OPENCLAW_STATE_DIR": str(state / "openclaw"),
                    "OPENCLAW_CONFIG_PATH": str(state / "openclaw/openclaw.json"),
                    "OPENCLAW_DISABLE_BUNDLED_PLUGIN_POSTINSTALL": "1",
                    "USERPROFILE": str(state / "openclaw/windows-home"),
                    "HOME": str(state / "openclaw/windows-home"),
                },
            ),
            "hermes": (
                [exe["hermes"], "-m", "hermes_cli.main", "gateway", "run"],
                {
                    "HERMES_HOME": str(state / "hermes"),
                    "OPENAI_API_KEY": secret["model_key"],
                    "OPENAI_BASE_URL": settings["model"]["base_url"],
                    "API_SERVER_ENABLED": "true",
                    "API_SERVER_KEY": secret["hermes"],
                    "API_SERVER_HOST": "127.0.0.1",
                    "API_SERVER_PORT": "8643",
                    "PYTHONPATH": str(Path(exe["hermes"]).parents[2]),
                    "GATEWAY_MULTIPLEX_PROFILES": "false",
                },
            ),
            "opencode": (
                [
                    exe["opencode"],
                    "serve",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    "4097",
                    "--print-logs",
                ],
                {
                    "OPENCODE_CONFIG": str(state / "opencode/opencode.json"),
                    "OPENCODE_CONFIG_DIR": str(state / "opencode"),
                    "XDG_CONFIG_HOME": str(state / "opencode/config"),
                    "XDG_DATA_HOME": str(state / "opencode/data"),
                    "XDG_CACHE_HOME": str(state / "opencode/cache"),
                    "XDG_STATE_HOME": str(state / "opencode/state"),
                    "OPENCODE_SERVER_PASSWORD": secret["opencode"],
                    "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
                    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
                },
            ),
        }
        for name, (command, extra) in launches.items():
            if names and name not in names:
                continue
            # Refuse to adopt or replace a port owned by any other process.
            port = int(settings["services"][name].rsplit(":", 1)[1])
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                (logs / f"{name}-blocked.log").write_text(
                    "Port already occupied; no process was stopped or adopted.\n",
                    encoding="utf-8",
                )
                continue
            except OSError:
                pass
            stdout = bounded_log(logs / f"{name}.stdout.log").open("ab")
            stderr = bounded_log(logs / f"{name}.stderr.log").open("ab")
            self.logs.extend((stdout, stderr))
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=WORKSPACE,
                    env={**env, **extra, "JUI_AGENT": name},
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if self.job is not None:
                    import win32api
                    import win32job

                    handle = win32api.OpenProcess(0x0100 | 0x0001, False, process.pid)
                    try:
                        win32job.AssignProcessToJobObject(self.job, handle)
                    finally:
                        handle.Close()
                self.children.append(process)
            except (OSError, RuntimeError) as error:
                stderr.write((type(error).__name__ + ": startup failed\n").encode())
                stderr.flush()

    async def close(self):
        if self.job is not None:
            import win32job

            win32job.TerminateJobObject(self.job, 0)
            self.job.Close()
        else:
            for child in self.children:
                if child.returncode is None:
                    child.terminate()
        for child in self.children:
            try:
                await asyncio.wait_for(child.wait(), 10)
            except TimeoutError:
                pass
        for log in self.logs:
            log.close()
