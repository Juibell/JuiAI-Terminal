"""Offline audio-file processing for the authenticated native client."""

import asyncio
import hmac
import json
import os
import subprocess
import uuid
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .command_worker import own_windows_job
from .config import ROOT, child_environment
from .control_api import control
from .language import localize_error

router = APIRouter(prefix="/api/v1/voice-processing")
DIRECTORY = ROOT / "02_Models/voice-isolator"
PYTHON = DIRECTORY / ".venv/Scripts/python.exe"
INWORLD = DIRECTORY / "isolate.py"
WEIGHTS = DIRECTORY / "weights/denoiser.safetensors"
UVR = DIRECTORY / "uvr_separate.py"
UVR_ROOT = Path(os.environ.get("JUI_UVR_ROOT", ROOT / "integrations/uvr"))
AUDIO_EXTENSIONS = {
    ".wav",
    ".flac",
    ".mp3",
    ".ogg",
    ".opus",
    ".m4a",
    ".aac",
    ".wma",
    ".aif",
    ".aiff",
    ".webm",
}

# Windows' venv launcher may spawn the interpreter before the parent can attach
# a job object. Hold the actual interpreter here until it is inside the job.
BOOTSTRAP = """
import os, runpy, sys
print(os.getpid(), flush=True)
if sys.stdin.buffer.read(1) != b'1':
    raise SystemExit(1)
sys.argv = sys.argv[1:]
sys.path.insert(0, os.path.dirname(sys.argv[0]))
runpy.run_path(sys.argv[0], run_name='__main__')
"""


class ProcessingInput(BaseModel):
    input_path: str = Field(min_length=1, max_length=32767)
    output_dir: str = Field(min_length=1, max_length=32767)
    mode: Literal["inworld", "uvr_inworld"] = "inworld"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    save_background: bool = True


def installation():
    packages = DIRECTORY / ".venv/Lib/site-packages"
    inworld_ready = all(
        path.is_file()
        for path in (
            PYTHON,
            INWORLD,
            WEIGHTS,
            packages / "inworld/voice_isolation/__init__.py",
            packages / "soundfile.py",
            packages / "torch/__init__.py",
        )
    )
    uvr_models = UVR_ROOT / "models/MDX_Net_Models"
    uvr_ready = inworld_ready and all(
        path.is_file()
        for path in (
            UVR,
            UVR_ROOT / "ffmpeg.exe",
            uvr_models / "UVR_MDXNET_Main.onnx",
            uvr_models / "model_data/model_data.json",
            packages / "onnxruntime/__init__.py",
        )
    )
    return {
        "inworld_ready": inworld_ready,
        "uvr_ready": uvr_ready,
        "uvr_root": str(UVR_ROOT),
    }


def local_path(value, *, directory=False):
    path = Path(value)
    if not path.is_absolute() or (os.name == "nt" and path.drive.startswith("\\\\")):
        raise ValueError("Choose an absolute path on a local drive")
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("The selected path is unavailable") from None
    if os.name == "nt" and path.drive.startswith("\\\\"):
        raise ValueError("Choose an absolute path on a local drive")
    if directory and path.is_dir():
        return path
    if not directory and path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
        return path
    raise ValueError(
        "Choose an existing output directory"
        if directory
        else "Choose a supported audio file"
    )


async def run_worker(script, args, on_line=None):
    env = child_environment()
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    launch = asyncio.create_task(
        asyncio.create_subprocess_exec(
            str(PYTHON),
            "-X",
            "utf8",
            "-I",
            "-u",
            "-c",
            BOOTSTRAP,
            str(script),
            *map(str, args),
            cwd=DIRECTORY,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            env=env,
        )
    )
    process = job = None
    try:
        process = await asyncio.shield(launch)
        pid = await asyncio.wait_for(process.stdout.readline(), 20)
        if not pid.strip().isdigit():
            raise ValueError("Could not start the audio worker")
        if os.name == "nt":
            job = own_windows_job(int(pid))
        process.stdin.write(b"1")
        await process.stdin.drain()
        process.stdin.close()

        last = ""
        while raw := await process.stdout.readline():
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                last = line
                if on_line:
                    on_line(line)
        if await process.wait():
            raise ValueError(
                last[:600] or "Audio worker exited before producing results"
            )
        return last
    finally:
        if process is None and not launch.cancelled():
            process = (await asyncio.gather(launch, return_exceptions=True))[0]
        if process is not None and not isinstance(process, BaseException):
            process.stdin.close()
            if job:
                job.Close()
            elif process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.communicate()


class VoiceProcessing:
    def __init__(self, center):
        self.center = center
        self.jobs = {}
        self.task = None
        self.active_id = None

    @property
    def running(self):
        return [self.active_id] if self.active_id else []

    def update(self, job, **changes):
        job.update(changes, revision=job["revision"] + 1)
        # The recent snapshots stay in memory. Paths and errors are private to
        # the bearer-protected job endpoint and never enter the event database.
        self.center.emit("voice_processing", id=job["id"], revision=job["revision"])

    def submit(self, body, voice):
        if self.active_id or self.center.maintenance:
            raise HTTPException(
                409, "Wait for the current audio job or maintenance to finish"
            )
        installed = installation()
        if not installed["inworld_ready"]:
            raise HTTPException(
                503, "The local Inworld Voice Isolator installation is incomplete"
            )
        if body.mode == "uvr_inworld" and not installed["uvr_ready"]:
            raise HTTPException(
                503, "The local Ultimate Vocal Remover installation is incomplete"
            )
        source = local_path(body.input_path)
        destination = local_path(body.output_dir, directory=True)
        identity = uuid.uuid4().hex
        job = {
            "id": identity,
            "revision": 0,
            "status": "queued",
            "stage": "waiting",
            "progress": None,
            "error": None,
            "output_dir": None,
            "voice_path": None,
            "background_path": None,
            "vocals_path": None,
            "instrumental_path": None,
            "device": None,
            "uvr_device": None,
            "sample_rate": None,
            "sample_count": None,
        }
        if len(self.jobs) >= 32:
            self.jobs.pop(next(iter(self.jobs)))
        self.jobs[identity] = job
        self.active_id = identity
        self.task = asyncio.create_task(self.run(job, body, source, destination, voice))
        self.task.add_done_callback(lambda task: self.finish(job, task))
        return dict(job)

    def finish(self, job, task):
        self.active_id = self.task = None
        if task.cancelled():
            self.update(job, status="cancelled", stage="finished", progress=None)
        elif error := task.exception():
            self.update(
                job,
                status="failed",
                stage="finished",
                progress=None,
                error=str(error)[:600] or type(error).__name__,
            )

    def cancel(self, identity):
        job = self.jobs[identity]
        if (
            identity == self.active_id
            and not self.task.done()
            and not self.task.cancelling()
        ):
            self.update(job, status="cancelling")
            self.task.cancel()
        return dict(job)

    async def close(self):
        task = self.task
        if task:
            self.cancel(self.active_id)
            await asyncio.gather(task, return_exceptions=True)

    async def run(self, job, body, source, destination, voice):
        def uvr_line(line):
            fields = line.split()
            if len(fields) == 2 and fields[0] == "UVR_DEVICE":
                self.update(job, uvr_device=fields[1])
            elif (
                len(fields) == 3
                and fields[0] == "UVR_PROGRESS"
                and all(field.isdecimal() for field in fields[1:])
            ):
                done, total = map(int, fields[1:])
                if 0 <= done <= total and total:
                    percent = round(100 * done / total)
                    if percent != job["progress"]:
                        self.update(job, progress=percent)

        with TemporaryDirectory(prefix=".jui-voice-", dir=destination) as temporary:
            staging = Path(temporary)
            files = {"voice_path": "voice.wav"}
            if body.save_background:
                files["background_path"] = "background.wav"
            if body.mode == "uvr_inworld":
                self.update(job, status="running", stage="uvr")
                await run_worker(
                    UVR,
                    (
                        "--input",
                        source,
                        "--vocals",
                        staging / "uvr-vocals.wav",
                        "--instrumental",
                        staging / "uvr-instrumental.wav",
                        "--uvr-root",
                        UVR_ROOT,
                        "--device",
                        "cpu",
                    ),
                    uvr_line,
                )
                files.update(
                    vocals_path="uvr-vocals.wav",
                    instrumental_path="uvr-instrumental.wav",
                )
                source = staging / "uvr-vocals.wav"
                self.update(job, stage="waiting", progress=None)

            args = [
                "--input",
                source,
                "--voice",
                staging / "voice.wav",
                "--device",
                body.device,
            ]
            if body.save_background:
                args.extend(("--background", staging / "background.wav"))
            use_gpu = body.device != "cpu"
            async with self.center.gpu_lock if use_gpu else nullcontext():
                if use_gpu:
                    await self.center.zonos.close()
                    await voice.wait_for_model_sleep()
                self.update(job, status="running", stage="inworld")
                result = json.loads(await run_worker(INWORLD, args))
            metadata = {
                field: result[field]
                for field in ("device", "sample_rate", "sample_count")
            }
            if any((staging / name).stat().st_size <= 44 for name in files.values()):
                raise ValueError(
                    "Audio processing did not produce complete WAV outputs"
                )
            final = destination / ("voice-isolator-" + job["id"][:12])
            if final.exists():
                raise ValueError("The output directory already exists")
            staging.rename(final)

        self.update(
            job,
            status="completed",
            stage="finished",
            progress=100,
            output_dir=str(final),
            **metadata,
            **{field: str(final / name) for field, name in files.items()},
        )


def processing(request):
    center = control(request)
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not token or not hmac.compare_digest(token, center.store.desktop_token()):
        raise HTTPException(401, "Open this feature in the native control center")
    return center.voice_processing


@router.get("/status")
async def voice_processing_status(request: Request):
    processing(request)
    return installation()


@router.post("/jobs", status_code=202)
async def submit_job(body: ProcessingInput, request: Request):
    return processing(request).submit(body, request.app.state.voice)


@router.get("/jobs/{identity}")
async def job_status(identity: str, request: Request):
    return localize_error(processing(request).jobs[identity])


@router.post("/jobs/{identity}/cancel")
async def cancel_job(identity: str, request: Request):
    return localize_error(processing(request).cancel(identity))
