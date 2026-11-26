import asyncio
import io

import httpx
import numpy as np
import soundfile as sf

from jui import voice as module


def test_memory_speech_and_cleanup(monkeypatch, tmp_path):
    voice = module.Voice(asyncio.Lock())
    voice.settings["tts"].update(
        output_dir=str(tmp_path), output_prefix="JuiAI/FishAudio/Workbench"
    )
    output_folder = tmp_path / "JuiAI/FishAudio/Workbench"
    output_folder.mkdir(parents=True)
    flac = io.BytesIO()
    sf.write(flac, np.zeros(2400), 24000, format="FLAC")
    item = {}

    def prompt(text, identity):
        item.update(
            filename=identity + "_00001.flac",
            subfolder="JuiAI/FishAudio/Workbench",
            type="output",
        )
        (output_folder / item["filename"]).write_bytes(flac.getvalue())
        return {"fake": text}

    def handler(request):
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "test"})
        if request.url.path == "/history/test":
            return httpx.Response(
                200, json={"test": {"outputs": {"3": {"audio": [item]}}}}
            )
        if request.url.path == "/view":
            return httpx.Response(200, content=flac.getvalue())
        raise AssertionError(request.url.path)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(voice, "prompt", prompt)

    preparation = []

    async def release_zonos():
        assert voice.lock.locked()
        preparation.append("released")

    async def sleeping():
        assert preparation == ["released"]

    voice.prepare_native_voice = release_zonos
    monkeypatch.setattr(voice, "wait_for_model_sleep", sleeping)
    audio = asyncio.run(voice.synthesize("test"))
    samples, rate = sf.read(io.BytesIO(audio))
    assert rate == 24000 and len(samples) == 2400
    assert audio.startswith(b"RIFF")
    assert not list(output_folder.iterdir())


def test_asr_does_not_need_a_vtuber_import():
    assert "open_llm_vtuber" not in module.__dict__
