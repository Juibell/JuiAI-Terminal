import asyncio
import io
import wave

import pytest
from fastapi.testclient import TestClient

from jui import plugins
from jui.voice import Voice
from jui.web import app


@pytest.mark.parametrize(
    "path",
    [
        "../settings.json",
        "../.runtime/secrets.json",
        "C:/Users",
        ".env",
        ".env.local",
        ".git/config",
        ".venv/test",
        "folder/../../bad",
    ],
)
def test_reject_private_paths(path, monkeypatch, tmp_path):
    monkeypatch.setattr(plugins, "WORKSPACE", tmp_path)
    with pytest.raises(ValueError):
        plugins.workspace_path(path)


def test_workspace_text_only(monkeypatch, tmp_path):
    monkeypatch.setattr(plugins, "WORKSPACE", tmp_path)
    monkeypatch.setattr(plugins, "enabled", lambda _: None)
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    (tmp_path / ".env").write_text("private", encoding="utf-8")
    assert plugins.read_file("hello.txt")["text"] == "hello"
    assert [entry["path"] for entry in plugins.list_files()["items"]] == ["hello.txt"]


def test_no_desktop_without_grant(monkeypatch):
    monkeypatch.setattr(plugins, "enabled", lambda _: None)
    monkeypatch.setattr(plugins, "desktop_allowed", lambda _: False)
    value = asyncio.run(plugins.desktop("search_news", {"query": "test"}))
    assert value["success"] is False
    assert value["status"] == "blocked"


def test_foreign_comfy_output_not_deleted(tmp_path):
    voice = Voice(asyncio.Lock())
    voice.settings["tts"].update(
        output_dir=str(tmp_path), output_prefix="JuiAI/FishAudio/Workbench"
    )
    foreign = tmp_path / "foreign.wav"
    foreign.write_bytes(b"keep")
    with pytest.raises(ValueError):
        voice.cleanup(
            {"filename": "foreign.wav", "subfolder": "", "type": "output"}, "jui_own"
        )
    assert foreign.exists()


def test_own_comfy_output_deleted(tmp_path):
    voice = Voice(asyncio.Lock())
    voice.settings["tts"].update(
        output_dir=str(tmp_path), output_prefix="JuiAI/FishAudio/Workbench"
    )
    output = tmp_path / "JuiAI/FishAudio/Workbench"
    output.mkdir(parents=True)
    own = output / "jui_test_00001.flac"
    own.write_bytes(b"temporary")
    voice.cleanup(
        {
            "filename": own.name,
            "subfolder": "JuiAI/FishAudio/Workbench",
            "type": "output",
        },
        "jui_test",
    )
    assert not own.exists()


def test_reject_non_pcm_audio():
    voice = Voice(asyncio.Lock())
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(b"\0" * 400)
    with pytest.raises(ValueError):
        voice.validate_audio(audio.getvalue())


def test_http_boundary():
    client = TestClient(app)
    assert client.get("/api/workspaces").status_code == 401
    assert client.get("/", headers={"host": "evil.example"}).status_code == 403
    assert client.get("/api/health").json()["native"]
    assert (
        client.get(
            "/api/health", headers={"origin": "https://evil.example"}
        ).status_code
        == 403
    )
