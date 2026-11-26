"""One recording per isolated process; never persist microphone audio."""

import contextlib
import io
import json
import sys
import wave

import numpy as np

from .config import load_settings


def samples_from_wav(audio, max_seconds):
    with wave.open(io.BytesIO(audio)) as wav:
        if (
            wav.getnchannels() != 1
            or wav.getsampwidth() != 2
            or wav.getframerate() != 16000
        ):
            raise ValueError("Expected 16 kHz mono PCM16 WAV")
        if wav.getnframes() > max_seconds * 16000:
            raise ValueError("Recording exceeds the duration limit")
        samples = (
            np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16).astype(
                np.float32
            )
            / 32768.0
        )
    if not len(samples):
        raise ValueError("Empty recording")
    return samples


def recognize(audio):
    settings = load_settings()["voice"]
    samples = samples_from_wav(audio, settings["max_seconds"])
    if settings["asr"]["model_type"] != "fireredasr2_llm":
        raise RuntimeError("Unsupported ASR configuration: expected FireRedASR2-LLM")
    from .firered import recognize as firered_recognize

    return firered_recognize(samples, settings["asr"])


if __name__ == "__main__":
    with contextlib.redirect_stdout(sys.stderr):
        result = recognize(sys.stdin.buffer.read(2_000_001))
    print(json.dumps({"text": result}, ensure_ascii=False))
