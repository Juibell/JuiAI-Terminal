import json
import secrets

import pytest

from jui import config, plugins


@pytest.fixture(autouse=True)
def isolated_plugin_grants(monkeypatch, tmp_path_factory):
    state = tmp_path_factory.mktemp("jui-plugin-state")
    (state / "secrets.json").write_text(
        json.dumps(
            {
                key: secrets.token_hex(16)
                for key in ("model_key", "openclaw", "hermes", "opencode")
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "STATE", state)
    monkeypatch.setattr(plugins, "STATE", state)
    monkeypatch.delenv("JUI_AGENT", raising=False)
