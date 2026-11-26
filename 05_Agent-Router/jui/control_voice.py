"""Shared voice transports for normal conversations and isolated profile tests."""

from contextlib import nullcontext

import httpx

from . import whisper_cpu


async def synthesize(c, voice_runtime, profile, text, voice=None, speed=None):
    if profile["provider"]["protocol"] == "native-tts":
        if voice is not None or speed is not None:
            raise ValueError("The existing Fish adapter uses its saved voice settings")
        return await voice_runtime.synthesize(text)
    async with c.gpu_lock if profile["provider"].get("local") else nullcontext():
        if c.zonos.matches(profile):
            return await c.zonos.synthesize(profile, text, voice, speed)
        async with httpx.AsyncClient(
            timeout=profile.get("timeout", 120), trust_env=False
        ) as client:
            response = await client.post(
                profile["provider"]["base_url"] + "/audio/speech",
                headers={"Authorization": "Bearer " + c.store.key(profile["provider"])},
                json={
                    "model": profile["model_id"],
                    "input": text,
                    "voice": voice or profile.get("voice", "alloy"),
                    "speed": speed if speed is not None else profile.get("speed", 1),
                    "response_format": "wav",
                },
            )
            response.raise_for_status()
            data = response.content
            if (
                len(data) > 50_000_000
                or data[:4] not in {b"RIFF", b"RF64"}
                or data[8:12] != b"WAVE"
            ):
                raise ValueError(
                    "The speech endpoint did not return a supported WAV response"
                )
            return data


async def transcribe(c, voice_runtime, profile, audio):
    if profile["provider"]["protocol"] == "native-asr":
        if whisper_cpu.matches(profile):
            if profile.get("model_id") != whisper_cpu.IDENTITY:
                raise ValueError(
                    "This profile requires the installed Whisper CPU model"
                )
            samples = voice_runtime.validate_audio(audio)
            async with voice_runtime.asr_lock:
                return await whisper_cpu.recognize(
                    audio, profile.get("language"), len(samples)
                )
        return await voice_runtime.recognize(audio)
    async with (
        c.gpu_lock if profile["provider"].get("local") else nullcontext(),
        httpx.AsyncClient(
            timeout=profile.get("timeout", 120), trust_env=False
        ) as client,
    ):
        response = await client.post(
            profile["provider"]["base_url"] + "/audio/transcriptions",
            headers={"Authorization": "Bearer " + c.store.key(profile["provider"])},
            files={"file": ("recording.wav", audio, "audio/wav")},
            data={
                "model": profile["model_id"],
                **(
                    {"language": profile["language"]} if profile.get("language") else {}
                ),
            },
        )
        response.raise_for_status()
        text = response.json()["text"]
        if not isinstance(text, str):
            raise TypeError("The recognition endpoint returned invalid text")
        return text
