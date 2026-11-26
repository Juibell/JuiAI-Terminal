import asyncio
import io
import json
import shutil
import subprocess
import sys
import time
import uuid
import wave
from pathlib import Path

import httpx
import soundfile as sf

from .asr_worker import samples_from_wav
from .config import APP, ROOT, child_environment, load_settings
from .control_local_models import wait_for_model_sleep


class Voice:
    def __init__(self, gpu_lock):
        self.settings = load_settings()["voice"]
        self.lock = gpu_lock
        self.asr_lock = asyncio.Lock()
        self.prepare_native_voice = None

    def validate_audio(self, audio):
        try:
            return samples_from_wav(audio, self.settings["max_seconds"])
        except (wave.Error, EOFError):
            raise ValueError("Only a valid PCM WAV recording is accepted") from None

    async def recognize(self, audio):
        self.validate_audio(audio)
        async with self.asr_lock, self.lock:
            if self.prepare_native_voice:
                await self.prepare_native_voice()
            await self.wait_for_model_sleep()
            return await self.run_asr_worker(audio)

    async def run_asr_worker(self, audio):
        config = self.settings["asr"]
        runtime = Path(config["runtime_dir"])
        packages = runtime / ".venv/Lib/site-packages"
        if not packages.is_dir():
            raise RuntimeError(
                "ASR Python environment is missing; run uv sync in its runtime folder"
            )
        bootstrap = "import site,sys,runpy; site.addsitedir(sys.argv[1]); sys.path.insert(0,sys.argv[2]); runpy.run_module('jui.asr_worker',run_name='__main__')"
        env = child_environment()
        env.update(
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            HF_HUB_DISABLE_PROGRESS_BARS="1",
        )
        worker = await asyncio.create_subprocess_exec(
            sys._base_executable,
            "-I",
            "-c",
            bootstrap,
            str(packages),
            str(APP),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env=env,
        )
        try:
            output, errors = await asyncio.wait_for(
                worker.communicate(audio), config.get("timeout_sec", 120)
            )
            if worker.returncode:
                log = ROOT / "12_Logs/Workbench/asr.stderr.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open("ab") as stream:
                    stream.write(errors[-8000:])
                raise RuntimeError(
                    "ASR worker failed; see 12_Logs/Workbench/asr.stderr.log"
                )
            try:
                result = json.loads(output)["text"]
                if not isinstance(result, str):
                    raise TypeError("ASR text is not a string")
            except (ValueError, KeyError, TypeError) as error:
                raise RuntimeError("ASR worker returned an invalid response") from error
            return result
        except BaseException:
            if worker.returncode is None:
                worker.kill()
                await worker.communicate()
            raise

    def prompt(self, text, identity):
        config = self.settings["tts"]
        source = Path(config["reference_audio"])
        target = Path(config["input_dir"]) / config["input_subdir"] / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or source.stat().st_size != target.stat().st_size:
            shutil.copy2(source, target)
        inputs = {
            name: config[name]
            for name in (
                "model_path",
                "language",
                "device",
                "precision",
                "attention",
                "max_new_tokens",
                "chunk_length",
                "temperature",
                "top_p",
                "repetition_penalty",
                "seed",
                "keep_model_loaded",
                "offload_to_cpu",
                "compile_model",
            )
        }
        inputs.update(
            text=text,
            reference_audio=["1", 0],
            reference_text=Path(config["reference_text"])
            .read_text(encoding="utf-8-sig")
            .strip(),
        )
        return {
            "1": {
                "class_type": "LoadAudio",
                "inputs": {
                    "audio": (Path(config["input_subdir"]) / source.name).as_posix()
                },
            },
            "2": {"class_type": "FishS2VoiceCloneTTS", "inputs": inputs},
            "3": {
                "class_type": "SaveAudio",
                "inputs": {
                    "audio": ["2", 0],
                    "filename_prefix": f"{config['output_prefix']}/{identity}",
                },
            },
        }

    def cleanup(self, item, identity):
        config = self.settings["tts"]
        output = Path(config["output_dir"]).resolve()
        own_folder = (output / config["output_prefix"]).resolve()
        path = (output / item.get("subfolder", "") / item["filename"]).resolve()
        path.relative_to(own_folder)
        if not path.name.startswith(identity) or item.get("type", "output") != "output":
            raise ValueError("Refusing to delete a foreign ComfyUI output")
        if config["cleanup_comfy_output"]:
            path.unlink(missing_ok=True)

    async def wait_for_model_sleep(self):
        await wait_for_model_sleep()

    async def synthesize(self, text):
        config = self.settings["tts"]
        identity = "jui_" + uuid.uuid4().hex
        async with self.lock:
            if self.prepare_native_voice:
                await self.prepare_native_voice()
            # The LLM and Fish share a 16 GB GPU. /props does not wake the LLM.
            await self.wait_for_model_sleep()
            async with httpx.AsyncClient(
                base_url=config["base_url"], timeout=120, trust_env=False
            ) as client:
                response = await client.post(
                    "/prompt",
                    json={
                        "prompt": self.prompt(text, identity),
                        "client_id": str(uuid.uuid4()),
                    },
                )
                response.raise_for_status()
                queued = response.json()
                if queued.get("node_errors") or not queued.get("prompt_id"):
                    raise ValueError("FishAudio rejected the voice workflow")
                prompt_id = queued["prompt_id"]
                deadline = time.monotonic() + config["timeout_sec"]
                item = None
                while time.monotonic() < deadline:
                    response = await client.get(f"/history/{prompt_id}")
                    response.raise_for_status()
                    history = response.json().get(prompt_id)
                    if history:
                        if history.get("status", {}).get("status_str") == "error":
                            raise ValueError("FishAudio generation failed")
                        # Only inspect our SaveAudio node, never other image/file outputs.
                        outputs = history.get("outputs", {}).get("3", {})
                        item = next(
                            (
                                entry
                                for key in ("audio", "images")
                                for entry in outputs.get(key, [])
                                if entry.get("filename")
                            ),
                            None,
                        )
                        break
                    await asyncio.sleep(config["poll_interval_sec"])
                if not item:
                    raise TimeoutError(
                        "FishAudio did not return audio before the deadline"
                    )
                try:
                    response = await client.get(
                        "/view",
                        params={
                            key: item.get(key, "output" if key == "type" else "")
                            for key in ("filename", "subfolder", "type")
                        },
                    )
                    response.raise_for_status()
                    audio, rate = sf.read(io.BytesIO(response.content), dtype="float32")
                    output = io.BytesIO()
                    sf.write(output, audio, rate, format="WAV", subtype="PCM_16")
                    return output.getvalue()
                finally:
                    self.cleanup(item, identity)
