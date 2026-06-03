#!/usr/bin/env python3
"""Install Codex hooks for Clawd Mochi Tank status animation."""

from __future__ import annotations

import json
import os
import shlex
import sys
import argparse
from pathlib import Path


DEFAULT_HOOKS_PATH = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "hooks.json"
STARTUP_SHORTCUT_NAME = "Clawd Hub App.lnk"
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


def pythonw_path() -> Path:
    python = Path(sys.executable)
    if os.name == "nt":
        candidate = python.with_name("pythonw.exe")
        if candidate.exists():
            return candidate
    return python


def ps_literal(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def install_startup_shortcut(app_script: Path) -> Path | None:
    if os.name != "nt":
        print("Startup shortcut skipped: only Windows startup links are supported by this installer.")
        return None
    try:
        import win32com.client  # type: ignore
        shell = win32com.client.Dispatch("WScript.Shell")
    except ImportError:
        import subprocess
        import textwrap
        startup = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        startup.mkdir(parents=True, exist_ok=True)
        shortcut = startup / STARTUP_SHORTCUT_NAME
        ps = textwrap.dedent(
            f"""
            $ws = New-Object -ComObject WScript.Shell
            $sc = $ws.CreateShortcut({ps_literal(shortcut)})
            $sc.TargetPath = {ps_literal(pythonw_path())}
            $sc.Arguments = '"' + {ps_literal(app_script)} + '" --minimized'
            $sc.WorkingDirectory = {ps_literal(app_script.parent)}
            $sc.WindowStyle = 7
            $sc.Description = 'Start Clawd Hook Hub background UI'
            $sc.Save()
            """
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
        return shortcut

    startup = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup.mkdir(parents=True, exist_ok=True)
    shortcut = startup / STARTUP_SHORTCUT_NAME
    sc = shell.CreateShortcut(str(shortcut))
    sc.TargetPath = str(pythonw_path())
    sc.Arguments = f'"{app_script}" --minimized'
    sc.WorkingDirectory = str(app_script.parent)
    sc.WindowStyle = 7
    sc.Description = "Start Clawd Hook Hub background UI"
    sc.Save()
    return shortcut


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


def remove_old_clawd_hooks(entries: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for entry in entries:
        hooks = [
            hook
            for hook in entry.get("hooks", [])
            if "codex_clawd_hook.py" not in str(hook.get("command", ""))
        ]
        if hooks:
            next_entry = dict(entry)
            next_entry["hooks"] = hooks
            cleaned.append(next_entry)
    return cleaned


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


def install(path: Path = DEFAULT_HOOKS_PATH, install_startup: bool = True) -> None:
    script = Path(__file__).with_name("codex_clawd_hook.py").resolve()
    app_script = Path(__file__).with_name("clawd_hub_app.py").resolve()
    command = command_for(script)

    settings = load_hooks(path)
    hooks = settings.setdefault("hooks", {})

    for event in HOOK_EVENTS:
        entries = remove_old_clawd_hooks(hooks.setdefault(event, []))
        entries.append(hook_entry(command, event))
        hooks[event] = entries

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"Installed Clawd Codex hooks in {path}")
    print(f"Command: {command}")
    if install_startup:
        shortcut = install_startup_shortcut(app_script)
        if shortcut:
            print(f"Installed Clawd Hub startup shortcut: {shortcut}")
    else:
        print("Skipped Clawd Hub startup shortcut.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-startup", action="store_true", help="do not create Windows Startup shortcut")
    args = parser.parse_args()
    install(install_startup=not args.no_startup)
