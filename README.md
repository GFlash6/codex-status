# Codex Clawd Status

Codex status bridge for the Clawd Mochi Tank ESP32 display.

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

Both paths are independent. CLI users typically only need native hooks.
VS Code / Desktop users need the watcher (native hooks may not fire there).

The Hub shares port 8765 with the Claude Code skill. Only one Hub process
should run at a time.

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
  [Adapt For Any Agent](#adapt-for-any-agent) below.

---

## Requirements

- ESP32 flashed with Clawd Mochi Tank firmware.
- Python 3.10+. On this machine: `C:\Python314\python.exe`.
- Optional Python packages (recommended):

  ```powershell
  python -m pip install pyserial bleak
  ```

  `pyserial` — USB serial auto-detection (CH340/CH341, ESP32 native CDC).  
  `bleak` — BLE Nordic UART transport. Falls back to serial if unavailable.

---

## Install (Codex)

**Step 1 — copy the skill** into the Codex skills directory:

```powershell
# From this repo:
Copy-Item -Recurse -Force `
  skills\codex-clawd-status `
  "$env:USERPROFILE\.codex\skills\codex-clawd-status"
```

**Step 2 — write hooks** into `~/.codex/hooks.json`:

```powershell
C:\Python314\python.exe `
  "$env:USERPROFILE\.codex\skills\codex-clawd-status\scripts\install_hooks.py"
```

**Step 3** — The installer immediately launches `clawd_hub_app.py --minimized`
in the background and creates a Windows Startup shortcut so the UI starts
automatically at every login. Use `--no-startup` to skip the shortcut:

```powershell
... install_hooks.py --no-startup
```

**Step 4 — restart Codex** and run `/hooks` in the CLI to review and
trust the hook command. Codex may prompt for trust again if the command
path changes.

**Step 5 — verify** `~/.codex/hooks.json` contains entries pointing at
`codex_clawd_hook.py` for each of the 10 hook events.

---

## Daily Start

Start the **UI controller** (keeps Hub and watcher alive, system tray):

```powershell
Start-Process -FilePath "C:\Python314\python.exe" `
  -ArgumentList @(
    "$env:USERPROFILE\.codex\skills\codex-clawd-status\scripts\clawd_hub_app.py",
    "--minimized"
  ) -WindowStyle Hidden
```

Start the **Hub** (animation router and ESP32 transport owner):

```powershell
Start-Process -FilePath "C:\Python314\python.exe" `
  -ArgumentList @(
    "$env:USERPROFILE\.codex\skills\codex-clawd-status\scripts\clawd_status_hub.py",
    "--transport", "auto"
  ) -WindowStyle Hidden
```

Start the **session watcher** (required for VS Code / Desktop sessions):

```powershell
Start-Process -FilePath "C:\Python314\python.exe" `
  -ArgumentList @(
    "$env:USERPROFILE\.codex\skills\codex-clawd-status\scripts\codex_session_watch.py",
    "--follow-latest"
  ) -WindowStyle Hidden
```

Dashboard: `http://127.0.0.1:8765`

If Claude Code is already running a Hub on port 8765, reuse it — do not
start a second Hub.

The hook script calls `ensure_hub()` and `ensure_session_watcher()`
automatically on first use, but manual startup is more reliable for
VS Code / Desktop hosts where native hooks may not fire.

---

## Test

```powershell
# Device and transport discovery:
C:\Python314\python.exe `
  "$env:USERPROFILE\.codex\skills\codex-clawd-status\scripts\codex_clawd_hook.py" `
  --doctor

# Send a test animation through Hub:
C:\Python314\python.exe `
  "$env:USERPROFILE\.codex\skills\codex-clawd-status\scripts\codex_clawd_hook.py" `
  --test thinking

# Check Hub state:
Invoke-RestMethod http://127.0.0.1:8765/state

# Check running Hub and watcher processes:
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*clawd_status_hub.py*' `
              -or $_.CommandLine -like '*codex_session_watch.py*' } |
  Select-Object ProcessId, CommandLine
```

---

## Event → Animation Mapping

| Codex event or session item | Animation |
| --- | --- |
| `SessionStart` | `idle` |
| `UserPromptSubmit` or session `user_message` | `thinking` |
| session `agent_message` | `thinking` |
| `PreToolUse` — shell/Bash/PowerShell | `building` |
| `PreToolUse` — Edit/Write/apply_patch | `typing` |
| `PreToolUse` — Read/Grep/Glob/search | `debugger` |
| `PreToolUse` — web/image generation | `wizard` |
| `PreToolUse` — Task/Agent/Subagent | `conducting` |
| `PreToolUse` — plan management | `juggling` |
| `PermissionRequest` | `confused` |
| `PostToolUse` or function output | `thinking` |
| `PreCompact` | `sweeping` |
| `PostCompact` | `thinking` |
| `Stop` or session `task_complete` | `happy` → `idle` → `sleeping` |
| `SubagentStart` | `conducting` |
| `SubagentStop` | `thinking` |

Lifecycle timing after task completion is configurable:

```powershell
$env:CLAWD_TANK_COMPLETE_ANIM    = "happy"
$env:CLAWD_TANK_IDLE_ANIM        = "idle"
$env:CLAWD_TANK_SLEEP_ANIM       = "sleeping"
$env:CLAWD_TANK_COMPLETE_SECONDS = "10"
$env:CLAWD_TANK_IDLE_SECONDS     = "30"
```

---

## Transport

Default: `auto` — tries BLE first, falls back to auto-detected serial.

```powershell
# Override transport:
$env:CLAWD_TANK_TRANSPORT = "serial"   # serial only
$env:CLAWD_TANK_TRANSPORT = "ble"      # BLE only
$env:CLAWD_TANK_TRANSPORT = "auto"     # BLE → serial (default)
```

Serial auto-detection scans port metadata for CH340/CH341 VID, Espressif
VID `303A`, and keywords ESP32/ESPRESSIF/USB JTAG/USB CDC. Do not
hard-code a COM port — use `CLAWD_TANK_SERIAL_PORT` only as a deliberate
override.

BLE device: `Claude-Mochi-Tank`  
Service UUID: `6e400001-b5a3-f393-e0a9-e50e24dcca9e`

---

## Client IDs on the Dashboard

| Client ID | Source |
| --- | --- |
| `codex-code` | Codex CLI native hook events |
| `codex-vscode` | Session watcher, `originator=codex_vscode` |
| `codex-desktop` | Session watcher, `originator=Codex Desktop` |
| `codex-watch` | Session watcher fallback |
| `manual` | Hub dashboard buttons |

Override native hook ID:

```powershell
$env:CLAWD_TANK_CLIENT_ID = "my-codex"
```

Override watcher fallback ID:

```powershell
$env:CLAWD_TANK_WATCH_CLIENT_ID = "my-codex-watch"
```

---

## Troubleshooting

**Hub page shows no events:**

1. `Invoke-RestMethod http://127.0.0.1:8765/health` — is Hub running?
2. For VS Code / Desktop: check the watcher process is running.
3. For CLI: check `~/.codex/hooks.json` points to `codex_clawd_hook.py`.
4. Restart Codex and run `/hooks` to trust the hook command.
5. Read `~/.clawd-mochi/status-hook.log`.

**Wrong source shown (e.g. `codex-watch` instead of `codex-vscode`):**

1. Check the first line of the current session JSONL for `originator`.
2. Restart `codex_session_watch.py --follow-latest` after script updates.

**Events in Hub but ESP32 does not change:**

1. Open dashboard, read `transport_message`.
2. BLE failing with "No Bluetooth adapter" and serial succeeding is normal fallback.
3. If serial also fails, close PlatformIO Serial Monitor or other apps holding the port.
4. Replug ESP32 and rerun `--doctor`.

**Bypass Hub for direct transport debugging:**

```powershell
... codex_clawd_hook.py --test typing --no-hub --transport serial
```

---

## Adapt For Any Agent

If you are **not** Codex but you want your agent's activity to appear on
the Clawd display, you only need to POST to the Hub. The hook scripts and
`install_hooks.py` are not required.

### Step 1 — Ensure the Hub is running

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
# expects: {"ok": true, "pid": ...}
```

If it is not running, start it:

```powershell
Start-Process -FilePath "C:\Python314\python.exe" `
  -ArgumentList @(
    "$env:USERPROFILE\.codex\skills\codex-clawd-status\scripts\clawd_status_hub.py",
    "--transport", "auto"
  ) -WindowStyle Hidden
```

### Step 2 — POST an animation to the Hub

The Hub accepts `POST http://127.0.0.1:8765/hook` with JSON:

```json
{
  "anim":        "thinking",
  "client_id":   "my-agent",
  "client_kind": "my-agent",
  "event":       "WorkStarted",
  "tool":        "some_tool"
}
```

| Field | Required | Description |
| --- | --- | --- |
| `anim` | **yes** | Animation name (see list below) |
| `client_id` | no | Identifier shown on dashboard (e.g. `"my-agent"`) |
| `client_kind` | no | Kind label (can match `client_id`) |
| `event` | no | Event name for dashboard display |
| `tool` | no | Tool name for dashboard display |

Available animations:
`idle` `thinking` `typing` `building` `debugger` `wizard`
`conducting` `juggling` `confused` `sweeping` `happy`
`sleeping` `beacon` `alert` `dizzy`

**PowerShell example:**

```powershell
Invoke-RestMethod http://127.0.0.1:8765/hook -Method Post `
  -ContentType "application/json" `
  -Body '{"anim":"thinking","client_id":"my-agent","event":"WorkStarted"}'
```

**Python example:**

```python
import json, urllib.request
req = urllib.request.Request(
    "http://127.0.0.1:8765/hook",
    data=json.dumps({"anim": "thinking", "client_id": "my-agent"}).encode(),
    headers={"Content-Type": "application/json"},
)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
opener.open(req, timeout=5)
```

### Step 3 — Wire into your platform's hook system

Most agent platforms let you run a shell command when events fire.
Write a small shim that maps your platform's event names to animation
names and POSTs to the Hub:

```python
#!/usr/bin/env python3
"""Minimal hook shim for any agent platform.

Receive event JSON on stdin, map to an animation, POST to Hub.
Exit 0 always so the agent is never blocked.
"""
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
    "idle":       "idle",
}

def post(anim: str, event: str = "", tool: str = "") -> None:
    body = json.dumps({
        "anim": anim,
        "client_id": "my-agent",   # change to your agent name
        "event": event,
        "tool": tool,
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8765/hook",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(req, timeout=3)
    except Exception:
        pass  # never block the agent

try:
    payload = json.loads(sys.stdin.read())
    event_name = payload.get("event", "")
    tool_name  = payload.get("tool", "")
    anim = EVENT_TO_ANIM.get(event_name, "thinking")
    post(anim, event=event_name, tool=tool_name)
except Exception:
    pass
```

Save this as `my_agent_hook.py`, make it executable, and register it
in your platform's hook configuration as a command that receives event
JSON on stdin. The key contract:

- The script reads event context from stdin (adapt if your platform uses argv or env).
- It POSTs `{"anim": ..., "client_id": ..., "event": ..., "tool": ...}` to Hub.
- It always exits 0 — display failures must never block the agent.

If your platform does not support stdin-based hooks, read the event
context from whatever source is available (env vars, a temp file, argv)
and build the same POST body.

### Step 4 — Verify on the dashboard

Open `http://127.0.0.1:8765` and confirm your `client_id` appears in
the Clients table and the animation changes when your agent is active.

---

## Files

```text
scripts/
  install_hooks.py          writes ~/.codex/hooks.json
  codex_clawd_hook.py       Codex native hook payload → animation → Hub
  codex_session_watch.py    tails ~/.codex/sessions/**/*.jsonl → Hub
  clawd_status_hub.py       Hub: HTTP server, transport owner, dashboard
  clawd_hub_app.py          background UI controller (tray / Tkinter)
references/
  hook-mapping.md           low-level payload field assumptions
SKILL.md                    full reference for Codex agents
```

Logs and runtime state: `%USERPROFILE%\.clawd-mochi\`
