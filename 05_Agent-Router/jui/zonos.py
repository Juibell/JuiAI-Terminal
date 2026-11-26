"""Owned, authenticated ZONOS3 runtime; weights are provisioned separately."""

import asyncio
import hashlib
import os
import secrets
import subprocess

import httpx

from .config import ROOT, child_environment
from .control_local_models import wait_for_model_sleep

DIRECTORY = ROOT / "03_TTS/ZONOS3-Mini"
MODELS = DIRECTORY / "models/gguf"
BASE = "http://127.0.0.1:1921"


def executable():
    return DIRECTORY / "build-cuda/zonos3-server.exe"


def install_profiles(store):
    if not executable().is_file():
        raise ValueError("Build the ZONOS3 runtime before registering its profiles")
    try:
        store.vault.get("zonos3-local")
    except FileNotFoundError:
        store.vault.put("zonos3-local", secrets.token_urlsafe(48))
    providers = {p["id"]: p for p in store.list("provider")}
    if "zonos3" not in providers:
        store.put(
            "provider",
            {
                "id": "zonos3",
                "name": "ZONOS3-Mini / Local CUDA",
                "protocol": "openai",
                "base_url": BASE + "/v1",
                "local": True,
                "enabled": True,
                "secret_ref": "zonos3-local",
            },
        )
    models = {m["id"] for m in store.list("model")}
    for identity, name, relative in (
        (
            "zonos3-american",
            "ZONOS3-Mini / Savannah",
            "English/Savannah_en-American_F_1.mp3",
        ),
        ("zonos3-anime", "ZONOS3-Mini / Sakura", "English/Sakura_en-anime_F_1.wav"),
    ):
        if identity in models or not (MODELS / "voices" / relative).is_file():
            continue
        store.put(
            "model",
            {
                "id": identity,
                "name": name,
                "provider_id": "zonos3",
                "model_id": "zonos3",
                "enabled": True,
                "capabilities": ["tts"],
                "timeout": 180,
                "voice": "default_" + hashlib.sha1(relative.encode()).hexdigest()[:16],
                "language": "en_us",
                "speed": 1.0,
            },
        )


class ZonosRuntime:
    def __init__(self, store):
        self.store = store
        self.process = None
        self.lock = asyncio.Lock()

    @staticmethod
    def matches(profile):
        provider = profile["provider"]
        return (
            provider.get("id") == "zonos3" and provider.get("base_url") == BASE + "/v1"
        )

    @property
    def owned(self):
        return self.process is not None and self.process.returncode is None

    def headers(self):
        provider = self.store.get("provider", "zonos3")
        if not provider.get("enabled"):
            raise ValueError("The ZONOS3 connection is disabled")
        key = self.store.key(provider)
        if not key:
            raise ValueError("The local ZONOS3 authentication key is missing")
        return {"Authorization": "Bearer " + key}

    async def ready(self):
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                response = await client.get(BASE + "/v1/models", headers=self.headers())
                response.raise_for_status()
                return any(
                    m.get("id") == "zonos3" for m in response.json().get("data", [])
                )
        except httpx.ConnectError:
            return False

    async def ensure(self):
        async with self.lock:
            if await self.ready():
                return
            if not self.owned:
                await wait_for_model_sleep()
                required = [
                    executable(),
                    MODELS / "backbone-ptq.gguf",
                    MODELS / "zac.gguf",
                ]
                if any(not path.is_file() for path in required):
                    raise ValueError(
                        "The local ZONOS3 runtime or its required weights are missing"
                    )
                logs = ROOT / "12_Logs/Workbench"
                logs.mkdir(parents=True, exist_ok=True)
                env = child_environment()
                env["ZONOS3_ACCESS_TOKEN"] = self.headers()["Authorization"][7:]
                env["PATH"] = os.pathsep.join(
                    [
                        str(DIRECTORY / "build-cuda/bin"),
                        *(
                            [os.path.join(env["CUDA_PATH"], "bin")]
                            if env.get("CUDA_PATH")
                            else []
                        ),
                        env.get("PATH", ""),
                    ]
                )
                with (
                    (logs / "zonos3.stdout.log").open("ab") as output,
                    (logs / "zonos3.stderr.log").open("ab") as errors,
                ):
                    self.process = await asyncio.create_subprocess_exec(
                        str(executable()),
                        str(MODELS / "backbone-ptq.gguf"),
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "1921",
                        "--gpu",
                        "--zac",
                        str(MODELS / "zac.gguf"),
                        "--spk-encoder",
                        str(MODELS / "spk-encoder.gguf"),
                        "--tts-default-voices-dir",
                        str(MODELS / "voices"),
                        "--batch",
                        "1",
                        "--zac-threads",
                        "1",
                        "--max",
                        "1200",
                        stdout=output,
                        stderr=errors,
                        stdin=asyncio.subprocess.DEVNULL,
                        cwd=DIRECTORY,
                        env=env,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
            for _ in range(120):
                if not self.owned:
                    raise RuntimeError(
                        "ZONOS3 exited during startup; inspect its local log"
                    )
                if await self.ready():
                    return
                await asyncio.sleep(0.5)
            await self.close()
            raise RuntimeError(
                "ZONOS3 did not become ready before the startup deadline"
            )

    async def voices(self):
        await self.ensure()
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.get(BASE + "/v2/speakers", headers=self.headers())
            response.raise_for_status()
            return [
                {"id": v["id"], "name": v["label"]}
                for v in response.json()["speakers"]
                if "embedding" in v["speaker_modes"]
            ]

    async def synthesize(self, profile, text, voice=None, speed=None):
        if (speed if speed is not None else profile.get("speed", 1)) != 1:
            raise ValueError(
                "ZONOS3 does not support the generic speed parameter; use its delivery controls instead"
            )
        try:
            return await self.generate(profile, text, voice)
        except (asyncio.CancelledError, httpx.TimeoutException):
            # Buffered generation can outlive a disconnected HTTP client. Free
            # our GPU worker before the caller releases the shared inference lock.
            await self.close()
            raise

    async def generate(self, profile, text, voice):
        await self.ensure()
        async with httpx.AsyncClient(
            timeout=profile.get("timeout", 180), trust_env=False
        ) as client:
            response = await client.post(
                BASE + "/v2/generate",
                headers=self.headers(),
                json={
                    "text": text,
                    "stream": False,
                    "format": "wav",
                    "speaker_mode": "embedding",
                    "speaker_embedding_id": voice or profile.get("voice"),
                    "language": profile.get("language") or "en_us",
                    "seed": 42,
                },
            )
            response.raise_for_status()
            return response.content

    async def close(self):
        if self.owned:
            process = self.process
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            await process.wait()
        self.process = None
