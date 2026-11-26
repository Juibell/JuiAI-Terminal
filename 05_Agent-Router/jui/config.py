import json
import os
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
ROOT = APP.parent
STATE = APP / ".runtime"
WORKSPACE = ROOT / "14_Workspace"


def load_settings():
    settings = json.loads((APP / "settings.json").read_text(encoding="utf-8-sig"))
    # Portable configuration paths are relative to the extracted application.
    for name in ("openclaw", "hermes", "opencode"):
        settings["executables"][name] = str(ROOT / settings["executables"][name])
    node = settings["executables"]["node"]
    if "/" in node or "\\" in node:
        settings["executables"]["node"] = str(ROOT / node)
    for section, fields in (
        ("tts", ("reference_audio", "reference_text", "input_dir", "output_dir")),
        ("asr", ("model_dir", "runtime_dir", "source_dir")),
    ):
        for field in fields:
            value = settings["voice"][section][field]
            settings["voice"][section][field] = str(ROOT / value)
    return settings


def load_secrets():
    return json.loads((STATE / "secrets.json").read_text(encoding="utf-8-sig"))


def child_environment():
    env = os.environ.copy()
    # No ambient cloud credentials, channels or external plugin configuration.
    for name in list(env):
        if name.endswith(("_API_KEY", "_TOKEN")) or name.startswith(
            ("OPENCLAW_", "HERMES_", "OPENCODE_")
        ):
            env.pop(name, None)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return env
