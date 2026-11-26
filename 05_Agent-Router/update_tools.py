"""Official tool updates, only when the workbench and unrelated tools are idle."""

import argparse
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from filelock import FileLock, Timeout

from jui.config import ROOT, STATE, load_settings
from jui.logs import bounded_log


def command(args, *, env=None, cwd=None, timeout=1800, logfile=None):
    result = subprocess.run(
        args,
        cwd=cwd or ROOT,
        env=env or os.environ.copy(),
        stdout=subprocess.PIPE,
        check=False,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    text = result.stdout.decode("utf-8", errors="replace")
    if logfile:
        with logfile.open("a", encoding="utf-8") as output:
            output.write(text)
    if result.returncode:
        raise RuntimeError(
            f"Update command failed ({result.returncode}); inspect the update log"
        )
    return text.strip()


def release(client, repo):
    response = client.get(f"https://api.github.com/repos/{repo}/releases/latest")
    if response.status_code in (403, 429):
        # GitHub's unauthenticated API can be rate limited. Its official
        # latest-release redirect needs no token and still excludes prereleases.
        response = client.get(
            f"https://github.com/{repo}/releases/latest", follow_redirects=True
        )
        response.raise_for_status()
        url = urlparse(str(response.url))
        prefix = f"/{repo}/releases/tag/"
        if url.hostname != "github.com" or not url.path.startswith(prefix):
            raise ValueError("Unverified official release redirect")
        version = url.path.removeprefix(prefix).removeprefix("v")
    else:
        response.raise_for_status()
        value = response.json()
        if value.get("prerelease") or value.get("draft"):
            raise ValueError("Refusing an unstable release")
        version = value["tag_name"].removeprefix("v")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Unexpected official version")
    return version


def version_file(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))["version"]


def installed_versions(settings=None):
    settings = settings or load_settings()
    exe = settings["executables"]
    result = {}
    for name, path in (
        ("openclaw", Path(exe["openclaw"]).parent / "package.json"),
        ("opencode", Path(exe["opencode"]).parents[1] / "package.json"),
    ):
        try:
            result[name] = version_file(path)
        except (OSError, ValueError, KeyError):
            result[name] = "unknown"
    try:
        result["hermes_commit"] = command(
            [
                "git",
                "-C",
                str(Path(exe["hermes"]).parents[2]),
                "rev-parse",
                "--short",
                "HEAD",
            ],
            timeout=15,
        )
        result["hermes"] = command(
            [
                "git",
                "-C",
                str(Path(exe["hermes"]).parents[2]),
                "describe",
                "--tags",
                "--always",
            ],
            timeout=15,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        result["hermes"] = "unknown"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    logs = ROOT / "12_Logs/Workbench"
    logs.mkdir(parents=True, exist_ok=True)
    logfile = bounded_log(logs / "updates.log")
    settings = load_settings()
    executable = settings["executables"]
    hermes_root = str(Path(executable["hermes"]).parents[2])
    result = {
        "at": datetime.now(UTC).isoformat(),
        "automatic": args.apply,
        "updated": [],
        "deferred": [],
        "failed": [],
    }
    latest = {}
    with httpx.Client(timeout=30, headers={"User-Agent": "Jui-Maintenance"}) as client:
        for name, repo in (
            ("openclaw", "openclaw/openclaw"),
            ("opencode", "anomalyco/opencode"),
            ("hermes", "NousResearch/hermes-agent"),
        ):
            try:
                latest[name] = release(client, repo)
            except (httpx.HTTPError, ValueError) as error:
                result["failed"].append(
                    {
                        "tool": name,
                        "error": type(error).__name__
                        + ": official release check failed",
                    }
                )
    result["latest_releases"] = latest
    result["installed"] = installed_versions(settings)
    if args.apply and not result["failed"]:
        # Do not stop a user-owned instance, nor update under any running runtime.
        owners = command(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "$processes=Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('node.exe','opencode.exe','hermes.exe','python.exe') -and $_.CommandLine -match 'OpenClaw|Hermes|OpenCode|opencode-ai|hermes_cli' }; @($processes | Select-Object ProcessId,Name) | ConvertTo-Json -Compress",
            ]
        )
        running = json.loads(owners) if owners else []
        if running:
            result["deferred"].append(
                "Agent runtimes are running. Close them before updating; no process was stopped."
            )
        else:
            env = os.environ.copy()
            for name in ("openclaw", "opencode", "hermes"):
                try:
                    if not Path(executable[name]).is_file():
                        result["deferred"].append(
                            f"{name} is not installed at its configured location."
                        )
                        continue
                    if (
                        name in ("openclaw", "opencode")
                        and result["installed"][name] == latest[name]
                    ):
                        continue
                    if (
                        name == "hermes"
                        and result["installed"].get("hermes", "").removeprefix("v")
                        == latest[name]
                    ):
                        continue
                    if name == "openclaw":
                        command(
                            [
                                "cmd.exe",
                                "/d",
                                "/c",
                                "npm.cmd",
                                "install",
                                "--prefix",
                                str(Path(executable["openclaw"]).parents[2]),
                                f"openclaw@{latest[name]}",
                                "--ignore-scripts",
                            ],
                            env=env,
                            logfile=logfile,
                        )
                        command(
                            [executable["node"], executable[name], "--version"],
                            env=env,
                            logfile=logfile,
                        )
                    elif name == "opencode":
                        # Windows self-upgrade can leave a JS shim named .exe when
                        # npm blocks postinstall. Install and verify explicitly.
                        command(
                            [
                                "cmd.exe",
                                "/d",
                                "/c",
                                "npm.cmd",
                                "install",
                                "--prefix",
                                str(Path(executable["opencode"]).parents[3]),
                                f"opencode-ai@{latest[name]}",
                                "--ignore-scripts",
                            ],
                            env=env,
                            logfile=logfile,
                        )
                        command(
                            [
                                executable["node"],
                                str(
                                    Path(executable["opencode"]).parents[1]
                                    / "postinstall.mjs"
                                ),
                            ],
                            env=env,
                            cwd=str(Path(executable["opencode"]).parents[3]),
                            logfile=logfile,
                        )
                        command(
                            [executable[name], "--version"], env=env, logfile=logfile
                        )
                    else:
                        dirty = command(
                            ["git", "-C", hermes_root, "status", "--porcelain"]
                        )
                        if dirty:
                            result["deferred"].append(
                                "Hermes has local modifications. Automatic source updates require review; no changes were stashed or discarded."
                            )
                            continue
                        # Use Python, not the self-replacing Windows hermes.exe.
                        env["HERMES_HOME"] = str(STATE / "control-agents/hermes")
                        env["PYTHONPATH"] = hermes_root
                        command(
                            [
                                executable[name],
                                "-m",
                                "hermes_cli.main",
                                "update",
                                "--yes",
                                "--no-backup",
                                "--channel",
                                "stable",
                                "--no-gateway-restart",
                            ],
                            env=env,
                            logfile=logfile,
                        )
                        command(
                            [executable[name], "-m", "hermes_cli.main", "--version"],
                            env=env,
                            logfile=logfile,
                        )
                        # Keep MCP, the HTTP API dependency and the existing ACP command available.
                        command(
                            [
                                "uv",
                                "sync",
                                "--project",
                                hermes_root,
                                "--python",
                                executable[name],
                                "--frozen",
                                "--no-dev",
                                "--extra",
                                "mcp",
                                "--extra",
                                "sms",
                                "--extra",
                                "acp",
                            ],
                            env=env,
                            logfile=logfile,
                        )
                    result["updated"].append(name)
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                    result["failed"].append({"tool": name, "error": str(error)})
    result["installed_after"] = installed_versions(settings)
    (logs / "updates.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        with FileLock(str(STATE / "agent-update.lock"), timeout=0):
            main()
    except Timeout:
        print("An Agent update/check is already running. No work was repeated.")
        raise SystemExit(2)
