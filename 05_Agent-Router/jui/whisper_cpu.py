"""Optional Whisper CPU profile. WAV bytes go through stdin, never a file."""

import asyncio
import subprocess

from .config import ROOT, child_environment

IDENTITY = "whisper-turbo-cpu"
DIRECTORY = ROOT / "04_ASR/Whisper-Large-v3-Turbo"
EXECUTABLE = DIRECTORY / "whisper.cpp-b5454-cpu-x64/Release/whisper-cli.exe"
WEIGHTS = DIRECTORY / "ggml-large-v3-turbo-q5_0.bin"


def matches(profile):
    return profile.get("provider", {}).get("id") == IDENTITY


def install_profile(store):
    if not EXECUTABLE.is_file() or not WEIGHTS.is_file():
        raise ValueError("Install the Whisper CPU runtime and weights first")
    for kind, value in (
        (
            "provider",
            {
                "name": "Whisper / Local CPU",
                "protocol": "native-asr",
                "base_url": "",
                "local": True,
                "enabled": True,
            },
        ),
        (
            "model",
            {
                "name": "Whisper large-v3-turbo Q5 / CPU",
                "provider_id": IDENTITY,
                "model_id": IDENTITY,
                "language": "auto",
                "enabled": True,
                "capabilities": ["asr"],
                "timeout": 60,
            },
        ),
    ):
        try:
            store.get(kind, IDENTITY)
        except KeyError:
            store.put(kind, {"id": IDENTITY, **value})


async def recognize(audio, language="auto", sample_count=None):
    language = language or "auto"
    if language not in {"auto", "en", "zh"}:
        raise ValueError("Whisper accepts auto, en or zh for this profile")
    if not EXECUTABLE.is_file() or not WEIGHTS.is_file():
        raise RuntimeError("Whisper CPU runtime or weights are missing")
    # With stdin input, '-' would otherwise also suppress the Windows print callback.
    context = (
        min(1500, max(512, (sample_count + 319) // 320 + 96)) if sample_count else 1500
    )
    worker = await asyncio.create_subprocess_exec(
        str(EXECUTABLE),
        "-m",
        str(WEIGHTS),
        "-ng",
        "-nt",
        "-t",
        "6",
        "-ac",
        str(context),
        "-of",
        "jui-memory",
        "-l",
        language,
        "-f",
        "-",
        cwd=EXECUTABLE.parent,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
        env=child_environment(),
    )
    try:
        stdout, _ = await asyncio.wait_for(worker.communicate(audio), timeout=60)
        if worker.returncode:
            raise RuntimeError("Whisper CPU worker failed")
        return stdout.decode("utf-8").strip()
    finally:
        if worker.returncode is None:
            try:
                worker.kill()
            except ProcessLookupError:
                pass
            await worker.communicate()
