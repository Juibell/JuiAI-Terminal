"""Lifecycle of optional local llama.cpp chat runtimes."""

import asyncio
import json
import subprocess
import time

import httpx

from .config import ROOT, load_secrets, load_settings

QWEN_ID = "qwen38"
QWEN_NAME = "Qwen3.8-27B Heretic ARA i1-IQ3_M"
QWEN_ALIAS = "jui-qwen38-27b"
QWEN_BASE = "http://127.0.0.1:8086/v1"
QWEN_CONTEXT = 4096
MANAGED_MODELS = {
    "bonsai": ("Start-Bonsai2.ps1", "Bonsai2-Main"),
    QWEN_ID: ("Start-Qwen38.ps1", "Qwen38-27B"),
}


def configured_model(identity):
    if identity == QWEN_ID:
        return {"base_url": QWEN_BASE, "id": QWEN_ALIAS, "context": QWEN_CONTEXT}
    return load_settings()["model"]


def managed_model(profile):
    identity = profile["provider"].get("id")
    if (
        identity in MANAGED_MODELS
        and profile["model_id"] == configured_model(identity)["id"]
    ):
        return identity
    return None


async def wait_for_model_sleep(exclude=None):
    """Wait for loaded chat weights to leave the shared GPU without waking them."""
    pending = {
        identity: configured_model(identity)["base_url"].removesuffix("/v1") + "/props"
        for identity in MANAGED_MODELS
        if identity != exclude
    }
    headers = {"Authorization": "Bearer " + load_secrets()["model_key"]}
    deadline = time.monotonic() + 65
    async with httpx.AsyncClient(
        timeout=5, trust_env=False, follow_redirects=False
    ) as client:
        while pending:
            for identity, url in list(pending.items()):
                try:
                    response = await client.get(url, headers=headers)
                    response.raise_for_status()
                    if response.json().get("is_sleeping") is not True:
                        continue
                except httpx.ConnectError:
                    pass  # A stopped service is already out of GPU memory.
                pending.pop(identity)
            if not pending:
                return
            if time.monotonic() >= deadline:
                raise ValueError(
                    "A local chat model has not released GPU memory. Wait for its current work to finish and try again."
                )
            await asyncio.sleep(1)


class ManagedModels:
    def __init__(self, store, zonos):
        self.store = store
        self.zonos = zonos
        self.started = {}

    async def ensure(self, profile):
        identity = managed_model(profile)
        if identity is None:
            return
        await self.zonos.close()
        await wait_for_model_sleep(exclude=identity)
        provider = profile["provider"]
        headers = {"Authorization": "Bearer " + self.store.key(provider)}
        async with httpx.AsyncClient(
            timeout=5, trust_env=False, follow_redirects=False
        ) as client:
            try:
                response = await client.get(
                    provider["base_url"] + "/models", headers=headers
                )
            except httpx.ConnectError:
                pass
            else:
                response.raise_for_status()
                if not any(
                    item.get("id") == profile["model_id"]
                    for item in response.json().get("data", [])
                ):
                    raise ValueError(
                        "The configured local port is serving a different model"
                    )
                return

        # If the caller cancels during startup, finish recording ownership before
        # allowing other GPU work or shutdown to proceed.
        start = asyncio.create_task(self.start(identity))
        try:
            await asyncio.shield(start)
        except asyncio.CancelledError:
            await asyncio.gather(start, return_exceptions=True)
            raise

    async def start(self, identity):
        output = await self.launch(identity, "Start")
        if output.get("StartedNew"):
            self.started[identity] = output["Pid"]

    async def launch(self, identity, action):
        script, _ = MANAGED_MODELS[identity]
        if not (ROOT / script).is_file():
            raise ValueError(
                "Start the model server separately or add your provider under Models & Connections."
            )
        logs = ROOT / "12_Logs/Workbench"
        logs.mkdir(parents=True, exist_ok=True)
        name = "launch" if action == "Start" else "stop"
        result_path = logs / f"{identity}-{name}.json"
        with (
            result_path.open("wb") as output,
            (logs / f"{identity}-{name}.stderr.log").open("wb") as errors,
        ):
            process = await asyncio.create_subprocess_exec(
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / script),
                "-Action",
                action,
                "-AsJson",
                stdout=output,
                stderr=errors,
                stdin=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await (
                process.wait()
            )  # Each launcher's readiness and stop operations are bounded.
        if process.returncode:
            label = "Qwen3.8" if identity == QWEN_ID else "Bonsai"
            raise ValueError(
                f"{label} could not {action.lower()}. See {identity}-{name}.stderr.log in 12_Logs/Workbench."
            )
        if action == "Start":
            return json.loads(result_path.read_text(encoding="utf-8-sig"))
        return {}

    async def close(self):
        for identity, pid in list(self.started.items()):
            _, directory = MANAGED_MODELS[identity]
            path = ROOT / "12_Logs" / directory / "process.json"
            try:
                record = json.loads(path.read_text(encoding="utf-8-sig"))
            except FileNotFoundError:
                continue
            if record.get("Running") and record.get("Pid") == pid:
                # The launcher also verifies the recorded executable and creation time.
                await self.launch(identity, "Stop")
            self.started.pop(identity)
