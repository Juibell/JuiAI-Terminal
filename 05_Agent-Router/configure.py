"""Configure isolated native agent profiles without legacy framework imports."""

import json
import secrets

import yaml

from jui.config import APP, STATE, WORKSPACE
from jui.credentials import private_directory


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main():
    private_directory(STATE)
    secret_file = STATE / "secrets.json"
    settings_file = APP / "settings.json"
    if not settings_file.exists():
        raise RuntimeError(
            "JuiAI Terminal requires settings.json"
        )
    if not secret_file.exists():
        save(
            secret_file,
            {
                "model_key": secrets.token_urlsafe(32),
                **{
                    name: secrets.token_urlsafe(32)
                    for name in ("openclaw", "hermes", "opencode")
                },
            },
        )
    secret = json.loads(secret_file.read_text(encoding="utf-8"))
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    for name in ("openclaw", "hermes", "opencode"):
        (STATE / name).mkdir(exist_ok=True)
    WORKSPACE.mkdir(exist_ok=True)
    python = str(APP / ".venv/Scripts/python.exe")
    mcp_script = str(APP / "mcp_server.py")
    mcp = {"command": python, "args": [mcp_script], "cwd": str(APP)}
    model = settings["model"]
    save(
        STATE / "openclaw/openclaw.json",
        {
            "gateway": {
                "mode": "local",
                "bind": "loopback",
                "port": 18790,
                "auth": {"mode": "token", "token": secret["openclaw"]},
                "http": {
                    "endpoints": {
                        "chatCompletions": {"enabled": True},
                        "responses": {"enabled": True},
                    }
                },
            },
            "agents": {
                "defaults": {
                    "workspace": str(WORKSPACE),
                    "model": {"primary": "jui/jui-bonsai2"},
                    "contextTokens": model["context"],
                    "maxConcurrent": 1,
                    "heartbeat": {"every": "0m"},
                }
            },
            "models": {
                "mode": "replace",
                "providers": {
                    "jui": {
                        "baseUrl": model["base_url"],
                        "apiKey": secret["model_key"],
                        "api": "openai-completions",
                        "models": [
                            {
                                "id": model["id"],
                                "name": "Jui Bonsai 2",
                                "contextWindow": model["context"],
                                "maxTokens": 3072,
                                "reasoning": True,
                                "input": ["text"],
                                "cost": {
                                    "input": 0,
                                    "output": 0,
                                    "cacheRead": 0,
                                    "cacheWrite": 0,
                                },
                            }
                        ],
                    }
                },
            },
            # Built-in profiles intersect MCP allowlists. Grant these exact names
            # directly, without granting any built-in command or filesystem tools.
            "tools": {
                "allow": [
                    f"jui__jui_{name}"
                    for name in (
                        "list_files",
                        "read_file",
                        "git_status",
                        "inspect_browser",
                        "open_google",
                        "search_news",
                        "open_project_folder",
                    )
                ],
                "deny": [
                    "exec",
                    "process",
                    "write",
                    "edit",
                    "apply_patch",
                    "browser",
                    "message",
                    "cron",
                    "gateway",
                    "sessions_spawn",
                    "sessions_send",
                ],
            },
            "mcp": {
                "servers": {
                    "jui": {
                        **mcp,
                        "env": {"JUI_AGENT": "openclaw"},
                        "transport": "stdio",
                        "enabled": True,
                        "requestTimeoutMs": 80000,
                    }
                }
            },
            "plugins": {"enabled": False},
            "browser": {"enabled": False},
        },
    )
    hermes = {
        "model": {
            "default": model["id"],
            "provider": "custom",
            "base_url": model["base_url"],
            "context_length": model["context"],
        },
        "agent": {"max_turns": 8},
        "platform_toolsets": {"api_server": ["jui"], "cli": ["jui"]},
        "mcp_servers": {"jui": {**mcp, "env": {"JUI_AGENT": "hermes"}}},
        "fallback_providers": [],
        "gateway": {
            "multiplex_profiles": False,
            "api_server": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 8643,
                "max_concurrent_runs": 1,
            },
        },
        "display": {"interim_assistant_messages": False, "tool_progress": "off"},
        "auxiliary": {"title_generation": {"enabled": False}},
        "terminal": {"backend": "local", "cwd": str(WORKSPACE)},
    }
    (STATE / "hermes/config.yaml").write_text(
        yaml.safe_dump(hermes, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    save(
        STATE / "opencode/opencode.json",
        {
            "$schema": "https://opencode.ai/config.json",
            "model": "jui/jui-bonsai2",
            "small_model": "jui/jui-bonsai2",
            "enabled_providers": ["jui"],
            "share": "disabled",
            "autoupdate": False,
            "provider": {
                "jui": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Jui Local",
                    "options": {
                        "baseURL": model["base_url"],
                        "apiKey": "{env:JUI_MODEL_KEY}",
                    },
                    "models": {
                        model["id"]: {
                            "name": "Bonsai 2 27B",
                            "limit": {"context": model["context"], "output": 3072},
                        }
                    },
                }
            },
            "permission": {
                "*": "deny",
                "read": "deny",
                "glob": "deny",
                "grep": "deny",
                "list": "deny",
                "jui_*": "allow",
            },
            "mcp": {
                "jui": {
                    "type": "local",
                    "command": [python, mcp_script],
                    "environment": {"JUI_AGENT": "opencode"},
                    "enabled": True,
                    "timeout": 80000,
                }
            },
            "plugin": [(APP / "plugins/opencode-audit.mjs").as_uri()],
            "agent": {
                "jui": {
                    "description": "Read-only Jui development specialist",
                    "mode": "primary",
                    "prompt": "You are Jui's code specialist. Reply in English unless the user requests another language. Only use jui MCP read-only workspace and git tools. Never use bash, write, edit, network fetching or arbitrary paths. Provide proposed code changes in your answer, never claim you changed files.",
                }
            },
        },
    )
    print(
        "Jui profiles configured. Secrets were not printed. Existing standalone tool profiles were not modified."
    )


if __name__ == "__main__":
    main()
