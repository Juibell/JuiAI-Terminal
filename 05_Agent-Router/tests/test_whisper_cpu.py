import asyncio
from types import SimpleNamespace

import pytest

from jui import control_voice, whisper_cpu


def test_whisper_transcribes_via_stdin_without_gpu_lock(monkeypatch, tmp_path):
    executable = tmp_path / "whisper-cli.exe"
    weights = tmp_path / "weights.bin"
    executable.touch()
    weights.touch()
    monkeypatch.setattr(whisper_cpu, "EXECUTABLE", executable)
    monkeypatch.setattr(whisper_cpu, "WEIGHTS", weights)
    audio = b"test-only PCM bytes"

    class Worker:
        returncode = 0

        async def communicate(self, data):
            assert data == audio
            return b"Two languages", b""

    async def create(*args, **kwargs):
        assert args[-4:] == ("-l", "zh", "-f", "-")
        assert "-ng" in args
        assert args[args.index("-of") + 1] == "jui-memory"
        assert not {"-otxt", "-oj"} & set(args)
        assert kwargs["stdin"] == asyncio.subprocess.PIPE
        return Worker()

    monkeypatch.setattr(whisper_cpu.asyncio, "create_subprocess_exec", create)
    voice = SimpleNamespace(asr_lock=asyncio.Lock(), validate_audio=lambda data: data)
    model = {
        "provider": {"id": whisper_cpu.IDENTITY, "protocol": "native-asr"},
        "model_id": whisper_cpu.IDENTITY,
        "language": "zh",
    }
    result = asyncio.run(control_voice.transcribe(None, voice, model, audio))
    assert result == "Two languages"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "weights.bin",
        "whisper-cli.exe",
    ]
    with pytest.raises(ValueError, match="auto, en or zh"):
        asyncio.run(whisper_cpu.recognize(audio, "invalid"))
