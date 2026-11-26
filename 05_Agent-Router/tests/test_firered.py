import asyncio
import io
import json
import wave
from pathlib import Path

import numpy as np
import pytest

from jui import voice as voice_module
from jui.firered import pcm_input, verify_assets


def recording(seconds=1):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(int(seconds * 16000) * 2))
    return buffer.getvalue()


def assets(tmp_path):
    names = [
        "model.pth.tar",
        "asr_encoder.pth.tar",
        "cmvn.ark",
        *[f"Qwen2-7B-Instruct/model-0000{i}-of-00004.safetensors" for i in range(1, 5)],
        *[
            "Qwen2-7B-Instruct/" + name
            for name in (
                "config.json",
                "tokenizer.json",
                "tokenizer_config.json",
                "model.safetensors.index.json",
            )
        ],
    ]
    for name in names:
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"fixture")
    (tmp_path / "download-manifest.json").write_text(
        json.dumps(
            {
                "repo": "FireRedTeam/FireRedASR2-LLM",
                "revision": "fixture",
                "files": [{"path": name, "size": 7} for name in names],
            }
        ),
        encoding="utf-8",
    )
    return {"model_dir": str(tmp_path), "revision": "fixture"}


def test_assets_require_both_speech_encoder_and_decoder(tmp_path):
    config = assets(tmp_path)
    assert verify_assets(config) == tmp_path
    (tmp_path / "Qwen2-7B-Instruct/model-00004-of-00004.safetensors").unlink()
    with pytest.raises(RuntimeError, match="missing/incomplete"):
        verify_assets(config)


def test_assets_reject_wrong_revision_and_truncated_encoder(tmp_path):
    config = assets(tmp_path)
    with pytest.raises(RuntimeError, match="manifest"):
        verify_assets(dict(config, revision="wrong"))
    (tmp_path / "model.pth.tar").write_bytes(b"short")
    with pytest.raises(RuntimeError, match="missing/incomplete"):
        verify_assets(config)


def test_kaldi_receives_original_pcm16_amplitudes():
    raw = np.array([-32768, -12000, 0, 14000, 32767], dtype=np.int16)
    rate, samples = pcm_input(raw.astype(np.float32) / 32768)
    assert rate == 16000
    assert samples.dtype == np.int16
    np.testing.assert_array_equal(raw, samples)


def test_main_server_never_imports_torch():
    from jui import voice

    assert "torch" not in voice.__dict__
    source = Path(voice.__file__).read_text(encoding="utf-8")
    assert "import torch" not in source


def test_gpu_asr_shares_lock_and_waits_for_bonsai_sleep(monkeypatch):
    async def run():
        lock = asyncio.Lock()
        voice = voice_module.Voice(lock)
        voice.settings["asr"]["model_type"] = "fireredasr2_llm"
        calls = []

        async def sleeping():
            assert lock.locked()
            calls.append("sleep")

        async def worker(audio):
            assert lock.locked() and audio.startswith(b"RIFF")
            calls.append("worker")
            return "recognized"

        monkeypatch.setattr(voice, "wait_for_model_sleep", sleeping)
        monkeypatch.setattr(voice, "run_asr_worker", worker)
        assert await voice.recognize(recording()) == "recognized"
        assert calls == ["sleep", "worker"]
        assert not lock.locked()

    asyncio.run(run())


def test_asr_duration_boundary():
    voice = voice_module.Voice(asyncio.Lock())
    voice.settings["max_seconds"] = 40
    assert len(voice.validate_audio(recording(40))) == 640000
    with pytest.raises(ValueError, match="duration"):
        voice.validate_audio(recording(40.01))


def test_worker_cancellation_releases_gpu_and_kills_child(monkeypatch, tmp_path):
    async def run():
        voice = voice_module.Voice(asyncio.Lock())
        voice.settings["asr"].update(
            model_type="fireredasr2_llm", runtime_dir=str(tmp_path)
        )
        (tmp_path / ".venv/Lib/site-packages").mkdir(parents=True)
        started = asyncio.Event()
        stopped = asyncio.Event()

        class Worker:
            returncode = None
            killed = False

            async def communicate(self, audio=None):
                assert audio is None or audio.startswith(b"RIFF")
                started.set()
                await stopped.wait()
                return b"", b""

            def kill(self):
                self.killed = True
                self.returncode = -1
                stopped.set()

        child = Worker()

        async def spawn(*args, **kwargs):
            assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
            assert str(tmp_path / ".venv/Lib/site-packages") in args
            return child

        async def sleeping():
            return None

        monkeypatch.setattr(voice_module.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(voice, "wait_for_model_sleep", sleeping)
        task = asyncio.create_task(voice.recognize(recording()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert child.killed and not voice.lock.locked()

    asyncio.run(run())


@pytest.mark.parametrize("output", [b"log line\n{}", b'{"text": 1}', b"{}"])
def test_worker_protocol_errors_are_not_reported_as_bad_audio(monkeypatch, output):
    class Worker:
        returncode = 0

        async def communicate(self, audio):
            return output, b""

    async def spawn(*args, **kwargs):
        return Worker()

    monkeypatch.setattr(voice_module.asyncio, "create_subprocess_exec", spawn)
    voice = voice_module.Voice(asyncio.Lock())
    # The real workbench runtime exists, but no GPU/imports/processes are used here.
    voice.settings["asr"]["runtime_dir"] = str(voice_module.APP)
    with pytest.raises(RuntimeError, match="invalid response"):
        asyncio.run(voice.run_asr_worker(recording()))
