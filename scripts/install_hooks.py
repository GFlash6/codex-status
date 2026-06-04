#!/usr/bin/env python3
"""Install Codex hooks for Clawd Mochi Tank status animation."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path


DEFAULT_HOOKS_PATH = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "hooks.json"
HOOK_EVENTS = [
    "SessionStart",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "UserPromptSubmit",
    "SubagentStart",
    "SubagentStop",
    "Stop",
]


def command_for(script: Path) -> str:
    python = Path(sys.executable)
    if os.name == "nt":
        return f'"{python}" "{script}"'
    return f"{shlex.quote(str(python))} {shlex.quote(str(script))}"


def hook_entry(command: str, event: str) -> dict:
    entry: dict = {
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": 5,
                "statusMessage": "Updating Clawd display",
            }
        ]
    }
    if event == "SessionStart":
        entry["matcher"] = "startup|resume|clear|compact"
    return entry


def load_hooks(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        backup = path.with_suffix(".json.bak")
        try:
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
        return {}


def install(path: Path = DEFAULT_HOOKS_PATH) -> None:
    script = Path(__file__).with_name("codex_clawd_hook.py").resolve()
    command = command_for(script)

    settings = load_hooks(path)
    hooks = settings.setdefault("hooks", {})

    for event in HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        already = any(
            command in h.get("command", "")
            for entry in entries
            for h in entry.get("hooks", [])
        )
        if not already:
            entries.append(hook_entry(command, event))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"Installed Clawd Codex hooks in {path}")
    print(f"Command: {command}")


if __name__ == "__main__":
    install()
