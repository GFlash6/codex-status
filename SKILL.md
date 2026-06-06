---
name: codex-clawd-status
description: Automatically use this skill whenever the user mentions Codex status lights, Clawd, Clawd Mochi Tank, hardware status indicators, Codex hooks, hook activation, permission/waiting/working/failed status display, Hook Hub, or debugging Codex-to-device status updates. Install and maintain Codex lifecycle hooks and the session watcher that translate Codex session, tool, permission, compact, stop, and subagent events into animation commands for a Clawd Mochi Tank ESP32 display.
---

# Codex Clawd Status

This skill connects Codex activity to the Clawd Mochi Tank ESP32 display.

---

## Architecture

```text
Codex CLI (hooks.json native hooks)          Codex VS Code / Desktop
  │  10 events: SessionStart,                  │  writes session JSONL to
  │  UserPromptSubmit, PreToolUse,             │  ~/.codex/sessions/**/*.jsonl
  │  PermissionRequest, PostToolUse,           │
  │  PreCompact, PostCompact, Stop,            ▼
  │  SubagentStart, SubagentStop             codex_session_watch.py
  │                                            tails newest session file,
  ▼                                            maps items → animations
codex_clawd_hook.py
  reads JSON payload from stdin,
  maps event+tool → animation name
  │                                            │
  └─────────────────┬───────────────────────── ┘
                    │ POST http://127.0.0.1:8765/hook
                    ▼
          clawd_status_hub.py         Hook Hub — receives deliveries,
                                       records state, drives transport
                    │
                    ├─── BLE Nordic UART ──────► ESP32 (Claude-Mochi-Tank)
                    └─── CH340/ESP32 serial ───► ESP32 (auto-detected)

          clawd_hub_app.py            Background UI controller.
                                       Keeps Hub and watcher alive,
                                       restartable from tray/window.
```

Two input paths feed the same Hub:

- **Native hooks** (`codex_clawd_hook.py`) — fired by Codex CLI when
  `~/.codex/hooks.json` is configured. Client ID: `codex-code`.
- **Session watcher** (`codex_session_watch.py`) — tails the newest
  JSONL session file written by Codex VS Code or Codex Desktop.
  Client ID: `codex-vscode`, `codex-desktop`, or `codex-watch`.

The Hub shares port 8765 with the Claude Code skill. Only one Hub process
should run at a time regardless of which skill directory it was started from.

---

## Is This Skill For You?

This skill targets **OpenAI Codex CLI** — the tool that stores hook
configuration in `~/.codex/hooks.json`.

**You are the right agent if:**

- You are Codex CLI (`codex` command), Codex VS Code extension, or Codex Desktop.
- Your hook config file is `%USERPROFILE%\.codex\hooks.json`.

**You are a different agent if:**

- You are Claude Code → use the `claude-clawd-status` skill instead.
- You are another LLM tool, CI runner, or custom agent → see
  [Adapt For Any Agent](#adapt-for-any-agent) at the end of this document.

---

## Requirements

- ESP32 is flashed with the Clawd Mochi Tank firmware.
- Python 3.10+ is available. On the current Windows setup this is typically `C:\Python314\python.exe`.
- Optional but recommended Python packages:

  ```powershell
  python -m pip install pyserial bleak
  ```

- `pyserial` enables ESP32 USB serial auto-detection, including CH340/CH341 adapters and native ESP32 USB CDC/JTAG ports.
- `bleak` enables BLE transport. If no Bluetooth adapter is available, `auto` transport falls back to serial.

---

## Files

Project copy:

```text
skills/codex-clawd-status/
```

Installed copy used by Codex:

```text
%USERPROFILE%\.codex\skills\codex-clawd-status\
```

Important scripts:

```text
scripts/install_hooks.py         writes ~/.codex/hooks.json
scripts/clawd_hub_app.py         background UI controller for Hub/watchers
scripts/codex_clawd_hook.py      handles native Codex hook payloads
scripts/codex_session_watch.py   tails ~/.codex/sessions/**/*.jsonl
scripts/clawd_status_hub.py      visual relay and transport owner
```

Runtime state and logs:

```text
%USERPROFILE%\.clawd-mochi\status-hook.log
%USERPROFILE%\.clawd-mochi\status-hub.log
%USERPROFILE%\.clawd-mochi\status-hub.pid
%USERPROFILE%\.clawd-mochi\session-watch.pid
```

---

## Install Or Update

1. Copy or install this skill into:

   ```text
   %USERPROFILE%\.codex\skills\codex-clawd-status\
   ```

2. Install Codex hook entries:

   ```powershell
   C:\Python314\python.exe C:\Users\admin\.codex\skills\codex-clawd-status\scripts\install_hooks.py
   ```

   From the project checkout:

   ```powershell
   python skills/codex-clawd-status/scripts/install_hooks.py
   ```

3. The installer immediately launches `clawd_hub_app.py --minimized` in the
   background. The Hub and session watcher start automatically from there.

4. Restart active Codex sessions.

5. In Codex CLI, run:

   ```text
   /hooks
   ```

   Review and trust the hook command. Codex may ask for trust again whenever the command path changes.

6. Verify `~/.codex/hooks.json` contains commands pointing at:

   ```text
   %USERPROFILE%\.codex\skills\codex-clawd-status\scripts\codex_clawd_hook.py
   ```

---

## Daily Start

The most reliable daily setup is to keep both Hub and watcher running
before opening Codex. Start the UI controller first — it manages both.

Start the background UI controller:

```powershell
Start-Process -FilePath "C:\Python314\python.exe" `
  -ArgumentList @(
    "C:\Users\admin\.codex\skills\codex-clawd-status\scripts\clawd_hub_app.py",
    "--minimized"
  ) `
  -WindowStyle Hidden
```

The UI controller keeps Hub and the Codex watcher alive, shows module status,
opens the dashboard, and can restart Hub, watcher, or BLE from a small window.
If `pystray` is installed it stays in the Windows system tray; without
`pystray` it falls back to Tkinter minimize behavior.

Start the Hub:

```powershell
Start-Process -FilePath "C:\Python314\python.exe" `
  -ArgumentList @(
    "C:\Users\admin\.codex\skills\codex-clawd-status\scripts\clawd_status_hub.py",
    "--transport", "auto"
  ) `
  -WindowStyle Hidden
```

Start the session watcher (required for VS Code / Desktop sessions):

```powershell
Start-Process -FilePath "C:\Python314\python.exe" `
  -ArgumentList @(
    "C:\Users\admin\.codex\skills\codex-clawd-status\scripts\codex_session_watch.py",
    "--follow-latest"
  ) `
  -WindowStyle Hidden
```

Open the dashboard:

```text
http://127.0.0.1:8765
```

Important limitation: if a Codex host never invokes native hooks, it cannot
trigger watcher autostart. Start `codex_session_watch.py --follow-latest`
manually for that host.

---

## Trigger Flow

Native hook flow:

```text
Codex event
  -> ~/.codex/hooks.json command
  -> codex_clawd_hook.py reads JSON from stdin
  -> payload_to_anim()
  -> deliver_anim()
  -> POST http://127.0.0.1:8765/hook
  -> Hub forwards to device
```

Native hook dashboard identity:

```text
client_id = codex-code
```

Session watcher flow:

```text
Codex VS Code / Codex Desktop writes ~/.codex/sessions/**/*.jsonl
  -> codex_session_watch.py tails newest session file
  -> item_to_anim()
  -> deliver_anim()
  -> POST http://127.0.0.1:8765/hook
  -> Hub forwards to device
```

Watcher dashboard identity is detected from the first JSONL line:

```text
session_meta.payload.originator = codex_vscode   -> codex-vscode
session_meta.payload.originator = Codex Desktop  -> codex-desktop
unknown                                            -> codex-watch
```

Override the native hook id:

```powershell
$env:CLAWD_TANK_CLIENT_ID = "my-codex"
```

Override the watcher fallback id:

```powershell
$env:CLAWD_TANK_WATCH_CLIENT_ID = "my-codex-watch"
```

---

## Hook Hub

Default Hub URL:

```text
http://127.0.0.1:8765
```

Endpoints:

```text
/        dashboard
/hook    hook/event intake  (POST JSON — see below)
/send    manual animation command
/state   current state JSON
/events  recent event history JSON
/health  liveness check
```

The Hub records:

- client connection and work status
- per-hook status
- current animation
- transport result
- recent event history

Hub localhost calls bypass system HTTP proxy settings so `HTTP_PROXY` and
`HTTPS_PROXY` do not break `127.0.0.1:8765`.

### POST /hook payload

```json
{
  "anim":        "thinking",
  "client_id":   "codex-code",
  "client_kind": "codex",
  "event":       "PreToolUse",
  "tool":        "shell_command"
}
```

Only `anim` is required. All other fields are optional metadata shown on
the dashboard. `anim` must be one of:
`idle` `thinking` `typing` `building` `debugger` `wizard`
`conducting` `juggling` `confused` `sweeping` `happy`
`sleeping` `beacon` `alert` `dizzy`

---

## Transport

Default:

```text
auto = BLE -> ESP32 serial
```

Supported values:

```text
auto         BLE, then ESP32 serial
parallel     send by BLE and ESP32 serial simultaneously
bluetooth    alias of ble
ble          BLE Nordic UART only
serial       ESP32 USB serial only
ble,serial   custom ordered fallback list
```

Set transport:

```powershell
$env:CLAWD_TANK_TRANSPORT = "auto"
```

Use serial only:

```powershell
C:\Python314\python.exe C:\Users\admin\.codex\skills\codex-clawd-status\scripts\codex_clawd_hook.py --test typing --transport serial
```

Serial detection:

- The script scans pyserial port metadata.
- It prefers CH340/CH341, Espressif VID `303A`, and fields containing `ESP32`, `ESPRESSIF`, `USB JTAG`, `USB CDC`, `USB SERIAL`, `USB-SERIAL`, or `CP210`.
- Do not hard-code COM ports in normal use.
- Use `CLAWD_TANK_SERIAL_PORT` only as a deliberate override.

BLE details:

```text
Device name: Claude-Mochi-Tank
Service UUID: 6e400001-b5a3-f393-e0a9-e50e24dcca9e
RX UUID:      6e400002-b5a3-f393-e0a9-e50e24dcca9e
TX UUID:      6e400003-b5a3-f393-e0a9-e50e24dcca9e
```

BLE payloads are newline-terminated JSON commands.

---

## Event Mapping

Default mapping:

| Codex event or session item | Animation |
| --- | --- |
| `SessionStart` | `idle` |
| `UserPromptSubmit` or session `user_message` | `thinking` |
| session `agent_message` | `thinking` |
| `PreToolUse` shell/code execution | `building` |
| `PreToolUse` edit/write/apply_patch | `typing` |
| `PreToolUse` read/search/inspect | `debugger` |
| `PreToolUse` web/image generation | `wizard` |
| `PreToolUse` task/subagent | `conducting` |
| `PreToolUse` task planning | `juggling` |
| `PermissionRequest` | `confused` |
| `PostToolUse` or function output | `thinking` |
| `PreCompact` | `sweeping` |
| `PostCompact` | `thinking` |
| `Stop` or session `task_complete` | `happy` |
| `SubagentStart` | `conducting` |
| `SubagentStop` | `thinking` |
| MCP/LSP-like calls | `beacon` |
| unknown tool | `typing` |

Lifecycle after completion:

```text
happy -> idle -> sleeping
```

Customize before starting Codex:

```powershell
$env:CLAWD_TANK_COMPLETE_ANIM    = "happy"
$env:CLAWD_TANK_IDLE_ANIM        = "idle"
$env:CLAWD_TANK_SLEEP_ANIM       = "sleeping"
$env:CLAWD_TANK_COMPLETE_SECONDS = "10"
$env:CLAWD_TANK_IDLE_SECONDS     = "30"
```

---

## Test

Check device discovery:

```powershell
C:\Python314\python.exe C:\Users\admin\.codex\skills\codex-clawd-status\scripts\codex_clawd_hook.py --doctor
```

Send a test animation through Hub:

```powershell
C:\Python314\python.exe C:\Users\admin\.codex\skills\codex-clawd-status\scripts\codex_clawd_hook.py --test thinking
```

Print mapping:

```powershell
C:\Python314\python.exe C:\Users\admin\.codex\skills\codex-clawd-status\scripts\codex_clawd_hook.py --print-mapping
```

Check running processes:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*clawd_status_hub.py*' `
              -or $_.CommandLine -like '*codex_session_watch.py*' } |
  Select-Object ProcessId, CommandLine
```

Check Hub:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
Invoke-RestMethod http://127.0.0.1:8765/state
Invoke-RestMethod http://127.0.0.1:8765/events
```

---

## Troubleshooting

Hub page has no events:

1. Check Hub is running:

   ```powershell
   Invoke-RestMethod http://127.0.0.1:8765/health
   ```

2. Check watcher is running for VS Code/Desktop sessions.
3. Check `~/.codex/hooks.json` points to the installed `codex_clawd_hook.py`.
4. Restart Codex and trust hooks with `/hooks`.
5. Read `~/.clawd-mochi/status-hook.log`.

Hub has events but ESP32 does not change:

1. Open the dashboard and inspect `transport_message`.
2. If BLE fails but serial succeeds, this is acceptable fallback behavior.
3. If serial fails, close PlatformIO Serial Monitor or any app holding the COM port.
4. Replug the ESP32 USB cable and rerun `--doctor`.

Events show the wrong Codex source:

1. Check the first line of the current session JSONL.
2. `originator=codex_vscode` should show as `codex-vscode`.
3. `originator=Codex Desktop` should show as `codex-desktop`.
4. Restart `codex_session_watch.py --follow-latest` after script updates.

Disable Hub for direct transport debugging:

```powershell
C:\Python314\python.exe C:\Users\admin\.codex\skills\codex-clawd-status\scripts\codex_clawd_hook.py --test typing --no-hub --transport serial
```

---

## Adapt For Any Agent

If you are not Codex but want your agent's activity to appear on the
Clawd display, you only need to POST to the Hub. No hook scripts or
`install_hooks.py` are required.

### Step 1 — Ensure the Hub is running

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
# expects: {"ok": true, "pid": ...}
```

If it is not running:

```powershell
Start-Process -FilePath "C:\Python314\python.exe" `
  -ArgumentList @(
    "C:\Users\admin\.codex\skills\codex-clawd-status\scripts\clawd_status_hub.py",
    "--transport", "auto"
  ) -WindowStyle Hidden
```

### Step 2 — POST an animation to the Hub

PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/hook -Method Post `
  -ContentType "application/json" `
  -Body '{"anim":"thinking","client_id":"my-agent","event":"WorkStarted"}'
```

Python:

```python
import json, urllib.request
body = json.dumps({"anim": "thinking", "client_id": "my-agent", "event": "WorkStarted"}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8765/hook", data=body,
    headers={"Content-Type": "application/json"},
)
urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3)
```

### Step 3 — Write a hook shim for your platform

```python
#!/usr/bin/env python3
"""Minimal hook shim — adapt event names and client_id for your agent."""
import json, sys, urllib.request

# Map your platform's event names to Clawd animations.
# Adjust this table to match your agent's event vocabulary.
EVENT_TO_ANIM = {
    "work_start": "thinking",
    "tool_use":   "building",
    "edit_file":  "typing",
    "read_file":  "debugger",
    "web_fetch":  "wizard",
    "task_done":  "happy",
    "error":      "dizzy",
}

def post(anim: str, event: str = "", tool: str = "") -> None:
    body = json.dumps({"anim": anim, "client_id": "my-agent",
                       "event": event, "tool": tool}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8765/hook", data=body,
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(req, timeout=3)
    except Exception:
        pass  # never block the agent

try:
    payload = json.loads(sys.stdin.read())
    anim = EVENT_TO_ANIM.get(payload.get("event", ""), "thinking")
    post(anim, event=payload.get("event", ""), tool=payload.get("tool", ""))
except Exception:
    pass
```

The key contract: always exit 0, never let display failures block the agent.

If your platform does not support stdin-based hooks, read event context from
env vars, argv, or a temp file and build the same POST body.

### Step 4 — Verify on the dashboard

Open `http://127.0.0.1:8765` and confirm your `client_id` appears in
the Clients table and animations change when your agent is active.

---

## Maintenance Notes

- Prefer editing the project copy (`skills/codex-clawd-status/`), then sync to `%USERPROFILE%\.codex\skills\codex-clawd-status`.
- Keep `codex_clawd_hook.py`, `codex_session_watch.py`, and `clawd_status_hub.py` behavior aligned.
- If the hook command changes, rerun `scripts/install_hooks.py`, restart Codex, and trust hooks again.
- Do not make the serial port fixed by default; ESP32 serial auto-detection is intentional.
- Keep Hub as the normal path so dashboard state remains accurate.

For lower-level payload assumptions, read `references/hook-mapping.md`.
