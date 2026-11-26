import asyncio
import io
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
import soundfile as sf
import test_control as shared
from fastapi import HTTPException

from jui.control_debug import tracked
from jui.voice import Voice

center = shared.center
client = shared.client
store = shared.store


@pytest.fixture(autouse=True)
def voice_runtime(center, monkeypatch):
    from jui.web import app

    voice = Voice(center.gpu_lock)
    monkeypatch.setattr(app.state, "voice", voice, raising=False)
    return voice


def wav():
    target = io.BytesIO()
    sf.write(target, np.zeros(1600), 16000, format="WAV", subtype="PCM_16")
    return target.getvalue()


def profile(center, identity="test-text", capabilities=None, local=True):
    provider = {
        "id": "test-connection",
        "name": "Test",
        "protocol": "openai",
        "base_url": "http://127.0.0.1:19000/v1" if local else "https://example.com/v1",
        "local": local,
        "enabled": True,
    }
    center.store.put("provider", provider)
    return center.store.put(
        "model",
        {
            "id": identity,
            "name": identity,
            "provider_id": provider["id"],
            "model_id": "test-model",
            "enabled": True,
            "capabilities": capabilities or ["text"],
            "voice": "test-voice",
            "speed": 1,
            "timeout": 5,
        },
    )


def test_text_debug_is_explicit_and_stateless(center, client, monkeypatch):
    profile(center)
    roles = center.store.get("roles", "global")
    captured = []

    async def completion(model, messages, **kwargs):
        captured.append((model, messages, kwargs))
        assert center.gpu_lock.locked()
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="Hello", tool_calls=None)
                )
            ]
        )

    monkeypatch.setattr(center.engine, "completion", completion)
    response = client.post(
        "/api/v1/debug/text",
        json={"model_ref": "test-text", "prompt": "Say hello", "request_id": "once"},
    )
    assert response.status_code == 200
    assert response.json()["text"] == "Hello"
    assert captured[0][0]["id"] == "test-text"
    assert captured[0][1] == [{"role": "user", "content": "Say hello"}]
    assert center.store.get("roles", "global") == roles
    assert center.store.list("conversation") == []
    assert not center.debug_workers
    assert (
        client.post(
            "/api/v1/debug/text",
            json={
                "model_ref": "test-text",
                "prompt": "Say hello",
                "request_id": "once",
            },
        ).status_code
        == 409
    )
    assert len(captured) == 1


def test_tools_debug_proposes_without_executing(center, client, monkeypatch):
    profile(center, capabilities=["text", "tools"])

    async def completion(model, messages, **kwargs):
        assert kwargs["tools"][0]["function"]["name"] == "debug_echo"
        assert kwargs["stream"] is False
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(
                                    name="debug_echo", arguments='{"text":"Hello"}'
                                )
                            )
                        ],
                    )
                )
            ]
        )

    monkeypatch.setattr(center.engine, "completion", completion)
    value = client.post(
        "/api/v1/debug/text",
        json={"model_ref": "test-text", "prompt": "Use the tool", "mode": "tools"},
    ).json()
    assert value["tools_executed"] is False
    assert value["tool_calls"][0]["name"] == "debug_echo"
    assert center.store.list("approval") == []


def test_wrong_capability_disabled_and_maintenance_are_blocked(center, client):
    model = profile(center)
    assert (
        client.post(
            "/api/v1/debug/speech", json={"model_ref": "test-text", "text": "Hello"}
        ).status_code
        == 400
    )
    center.store.put("model", {**model, "enabled": False})
    assert (
        client.post(
            "/api/v1/debug/text", json={"model_ref": "test-text", "prompt": "Hello"}
        ).status_code
        == 400
    )
    center.store.put("model", model)
    center.maintenance = True
    assert (
        client.post(
            "/api/v1/debug/text", json={"model_ref": "test-text", "prompt": "Hello"}
        ).status_code
        == 409
    )


def test_external_tests_require_confirmation_and_audio_policy(center, client):
    profile(center, capabilities=["text", "vision", "tts", "asr"], local=False)
    assert (
        client.post(
            "/api/v1/debug/text", json={"model_ref": "test-text", "prompt": "Hello"}
        ).status_code
        == 400
    )
    for path, data in (
        ("speech", {"text": "Hello"}),
        (
            "text",
            {
                "prompt": "Describe",
                "mode": "vision",
                "image": "data:image/png;base64,iVBORw0KGgo=",
            },
        ),
    ):
        response = client.post(
            "/api/v1/debug/" + path,
            json={"model_ref": "test-text", "confirm_external": True, **data},
        )
        assert response.status_code == 400
        assert "authorized" in response.text


def test_api_speech_uses_requested_profile_and_memory_wav(center, client, monkeypatch):
    from jui import control_voice

    profile(center, "test-tts", ["tts"])
    content = wav()
    captured = []

    def handle(request):
        assert center.gpu_lock.locked()
        captured.append(request)
        return httpx.Response(
            200, content=content, headers={"content-type": "audio/wav"}
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        control_voice.httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(handle), **kwargs),
    )
    response = client.post(
        "/api/v1/debug/speech",
        json={
            "model_ref": "test-tts",
            "text": "Good morning",
            "voice": "other-voice",
            "speed": 1.2,
        },
    )
    assert response.status_code == 200
    assert response.content == content
    assert b'"voice":"other-voice"' in captured[0].content
    assert b'"model":"test-model"' in captured[0].content
    assert center.store.resolve("tts")["id"] == "fish"


def test_native_asr_debug_returns_text_without_changing_binding(
    center, client, monkeypatch, voice_runtime
):
    async def recognize(data):
        assert data == wav()
        return "Good morning"

    monkeypatch.setattr(voice_runtime, "recognize", recognize)
    response = client.post(
        "/api/v1/debug/transcribe?model_ref=firered&request_id=recognition",
        content=wav(),
        headers={"content-type": "audio/wav"},
    )
    assert response.status_code == 200
    assert response.json()["text"] == "Good morning"
    assert center.store.resolve("asr")["id"] == "firered"
    assert (
        client.post(
            "/api/v1/debug/transcribe?model_ref=firered", content=b"invalid"
        ).status_code
        == 400
    )


def test_debug_cancellation_releases_worker(center):
    async def scenario():
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(control=center))
        )
        started = asyncio.Event()

        async def operation():
            started.set()
            await asyncio.sleep(100)

        pending = asyncio.create_task(tracked(request, "cancel-me", operation))
        await started.wait()
        center.debug_workers["cancel-me"].cancel()
        with pytest.raises(HTTPException) as error:
            await pending
        assert error.value.status_code == 409
        assert not center.debug_workers

    asyncio.run(scenario())


def test_vision_debug_uses_explicit_model_without_vision_binding(
    center, client, monkeypatch
):
    profile(center, capabilities=["vision"])

    async def completion(model, messages, **kwargs):
        assert model["id"] == "test-text"
        assert messages[0]["content"][1]["type"] == "image_url"
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="An image", tool_calls=None)
                )
            ]
        )

    monkeypatch.setattr(center.engine, "completion", completion)
    response = client.post(
        "/api/v1/debug/text",
        json={
            "model_ref": "test-text",
            "mode": "vision",
            "prompt": "Describe",
            "image": "data:image/png;base64,iVBORw0KGgo=",
        },
    )
    assert response.status_code == 200
    assert response.json()["text"] == "An image"
    assert center.store.get("roles", "global")["bindings"].get("vision") is None


def test_api_recognition_uses_selected_profile(center, client, monkeypatch):
    from jui import control_voice

    model = profile(center, "api-asr", ["asr"])
    center.store.put("model", {**model, "language": "en"})
    real = httpx.AsyncClient

    def handle(request):
        assert center.gpu_lock.locked()
        assert request.url.path == "/v1/audio/transcriptions"
        assert b"test-model" in request.content and b"recording.wav" in request.content
        return httpx.Response(200, json={"text": "Hello from API ASR"})

    monkeypatch.setattr(
        control_voice.httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(handle), **kwargs),
    )
    response = client.post("/api/v1/debug/transcribe?model_ref=api-asr", content=wav())
    assert response.status_code == 200
    assert response.json()["text"] == "Hello from API ASR"
    assert center.store.resolve("asr")["id"] == "firered"


def test_search_debug_does_not_change_preferences(center, client, monkeypatch):
    from jui import control_debug

    center.store.put(
        "provider",
        {
            "id": "search-test",
            "name": "Search",
            "enabled": True,
            "local": True,
            "protocol": "searxng",
            "base_url": "http://127.0.0.1:18888",
        },
    )
    before = center.store.get("preferences", "search")

    async def search(store, query, configuration):
        assert configuration["provider_id"] == "search-test"
        assert configuration["enabled"] is True
        return [{"title": query, "url": "https://example.com", "summary": "Test"}]

    monkeypatch.setattr(control_debug, "search", search)
    response = client.post(
        "/api/v1/debug/search", json={"provider_ref": "search-test", "query": "science"}
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["title"] == "science"
    assert center.store.get("preferences", "search") == before
    assert not center.debug_workers
    client.post("/api/v1/debug/search-late/cancel")
    response = client.post(
        "/api/v1/debug/search",
        json={
            "provider_ref": "search-test",
            "query": "science",
            "request_id": "search-late",
        },
    )
    assert response.status_code == 409


def test_debug_language_override_is_local_and_not_saved(center, client, monkeypatch):
    from jui.zonos import BASE

    model = profile(center, "sample", ["tts"])
    center.store.put(
        "provider",
        {
            "id": "zonos3",
            "protocol": "openai",
            "base_url": BASE + "/v1",
            "local": True,
            "enabled": True,
        },
    )
    center.store.put("model", {**model, "provider_id": "zonos3", "language": "en_us"})

    async def capture(c, voice_runtime, selected, text, voice, speed):
        assert selected["language"] == "cmn"
        assert text == "Hello"
        return wav()

    monkeypatch.setattr("jui.control_debug.synthesize", capture)
    result = client.post(
        "/api/v1/debug/speech",
        json={"model_ref": "sample", "text": "Hello", "language": "cmn"},
    )
    assert result.status_code == 200
    assert center.store.get("model", "sample")["language"] == "en_us"
    assert center.store.resolve("tts")["id"] == "fish"
    result = client.post(
        "/api/v1/debug/speech",
        json={"model_ref": "fish", "text": "Hello", "language": "en_gb"},
    )
    assert result.status_code == 400


def test_zonos_cancellation_stops_owned_worker(center, monkeypatch):
    async def scenario():
        runtime = center.zonos
        started = asyncio.Event()

        class Worker:
            returncode = None

            def terminate(self):
                self.returncode = -1

            async def wait(self):
                return self.returncode

        worker = Worker()
        runtime.process = worker

        async def generate(profile, text, voice):
            started.set()
            await asyncio.sleep(60)

        monkeypatch.setattr(runtime, "generate", generate)
        pending = asyncio.create_task(runtime.synthesize({}, "Hello"))
        await started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert worker.returncode == -1
        assert runtime.process is None

    asyncio.run(scenario())


def test_zonos_registration_is_idempotent_and_preserves_fish(
    center, tmp_path, monkeypatch
):
    from jui import zonos

    exe = tmp_path / "zonos3-server.exe"
    exe.touch()
    monkeypatch.setattr(zonos, "executable", lambda: exe)
    monkeypatch.setattr(zonos, "MODELS", tmp_path)
    voices = tmp_path / "voices/English"
    voices.mkdir(parents=True)
    (voices / "Savannah_en-American_F_1.mp3").touch()
    zonos.install_profiles(center.store)
    zonos.install_profiles(center.store)
    assert sum(m["id"] == "zonos3-american" for m in center.store.list("model")) == 1
    assert center.store.resolve("tts")["id"] == "fish"
    assert center.store.key(center.store.get("provider", "zonos3"))
