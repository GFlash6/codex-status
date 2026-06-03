---
name: codex-clawd-status
description: Automatically use this skill whenever the user mentions Codex status lights, Clawd, Clawd Mochi Tank, hardware status indicators, Codex hooks, hook activation, permission/waiting/working/failed status display, Hook Hub, or debugging Codex-to-device status updates. Install and maintain Codex lifecycle hooks and the session watcher that translate Codex session, tool, permission, compact, stop, and subagent events into animation commands for a Clawd Mochi Tank ESP32 display.
---

# Codex Clawd Status

This skill connects Codex activity to the Clawd Mochi Tank ESP32 display.

Runtime flow:

```text
Codex native hooks and/or Codex session JSONL watcher
  -> codex_clawd_hook.py / codex_session_watch.py
  -> local Hook Hub at http://127.0.0.1:8765
  -> BLE Nordic UART / auto-detected CH340 serial / HTTP
  -> ESP32 firmware
```

Use this document when installing the skill on a machine, refreshing hook configuration, starting the daily background processes, or debugging why the dashboard or display does not update.

## Requirements

- ESP32 is flashed with the Clawd Mochi Tank firmware.
- Python 3.10+ is available. On the current Windows setup this is typically `C:\Python314\python.exe`.
- Optional but recommended Python packages:
  ```powershell
  python -m pip install pyserial bleak
  ```
- `pyserial` enables CH340/CH341 USB serial auto-detection.
- `bleak` enables BLE transport. If no Bluetooth adapter is available, `auto` transport falls back to serial and then HTTP.

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

## Install Or Update

1. Copy or install this skill into:

   ```text
   %USERPROFILE%\.codex\skills\codex-clawd-status\
   ```

2. Install Codex hook entries:

   ```powershell
   C:\Python314\python.exe C:\Users\admin\.codex\skills\codex-clawd-status\scripts\install_hooks.py
   ```

   From the project checkout, use:

   ```powershell
   python skills/codex-clawd-status/scripts/install_hooks.py
   ```

   The installer also creates or updates this Windows Startup shortcut:

   ```text
   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Clawd Hub App.lnk
   ```

   The shortcut starts `clawd_hub_app.py --minimized` at login, so Hub and
   the watcher can stay available in the background. To install hooks without
   changing Startup entries, run:

   ```powershell
   python scripts/install_hooks.py --no-startup
   ```

3. Restart active Codex sessions.

4. In Codex CLI, run:

   ```text
   /hooks
   ```

   Review and trust the hook command. Codex may ask for trust again whenever the command path changes.

5. Verify `~/.codex/hooks.json` contains commands pointing at:

   ```text
   %USERPROFILE%\.codex\skills\codex-clawd-status\scripts\codex_clawd_hook.py
   ```

## Daily Start

After installation on Windows, the Hub UI controller is started automatically
at login from the `Clawd Hub App.lnk` Startup shortcut.

The most reliable daily setup is to keep both Hub and watcher running.

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
If `pystray` is installed it can stay in the Windows system tray; without
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

Start the session watcher:

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

The hook script can also auto-start both pieces:

- `codex_clawd_hook.py` calls `ensure_hub()` before delivering to Hub.
- `codex_clawd_hook.py` calls `ensure_session_watcher()` after it receives a real native hook payload.

Important limitation: if a Codex host never invokes native hooks, it cannot trigger watcher autostart. Start `codex_session_watch.py --follow-latest` manually for that host.

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

## Hook Hub

Default Hub URL:

```text
http://127.0.0.1:8765
```

Endpoints:

```text
/        dashboard
/hook    hook/event intake
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

Hub localhost calls bypass system HTTP proxy settings so `HTTP_PROXY` and `HTTPS_PROXY` do not break `127.0.0.1:8765`.

## Transport

Default:

```text
auto = BLE -> CH340 serial -> HTTP
```

Supported values:

```text
auto         BLE, then CH340 serial, then HTTP
parallel     send by BLE, CH340 serial, and HTTP
bluetooth    alias of ble
ble          BLE Nordic UART only
serial       CH340/CH341 USB serial only
http         HTTP only
serial,http  custom ordered fallback list
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
- It matches CH340/CH341 by VID `1A86` or fields containing `CH340`, `CH341`, `USB-SERIAL`, etc.
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

HTTP fallback default:

```text
http://192.168.4.1
```

Override:

```powershell
$env:CLAWD_TANK_URL = "http://192.168.4.1"
```

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
$env:CLAWD_TANK_COMPLETE_ANIM = "happy"
$env:CLAWD_TANK_IDLE_ANIM = "idle"
$env:CLAWD_TANK_SLEEP_ANIM = "sleeping"
$env:CLAWD_TANK_COMPLETE_SECONDS = "10"
$env:CLAWD_TANK_IDLE_SECONDS = "30"
```

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
  Where-Object { $_.CommandLine -like '*clawd_status_hub.py*' -or $_.CommandLine -like '*codex_session_watch.py*' } |
  Select-Object ProcessId, CommandLine
```

Check Hub:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
Invoke-RestMethod http://127.0.0.1:8765/state
Invoke-RestMethod http://127.0.0.1:8765/events
```

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
4. Replug CH340 USB and rerun `--doctor`.
5. If using HTTP fallback, connect to the ESP32 AP and check `http://192.168.4.1/state`.

Events show the wrong Codex source:

1. Check the first line of the current session JSONL.
2. `originator=codex_vscode` should show as `codex-vscode`.
3. `originator=Codex Desktop` should show as `codex-desktop`.
4. Restart `codex_session_watch.py --follow-latest` after script updates.

Disable Hub for direct transport debugging:

```powershell
C:\Python314\python.exe C:\Users\admin\.codex\skills\codex-clawd-status\scripts\codex_clawd_hook.py --test typing --no-hub --transport serial
```

## Maintenance Notes For Codex

- Prefer editing the project copy, then sync to `%USERPROFILE%\.codex\skills\codex-clawd-status`.
- Keep `codex_clawd_hook.py`, `codex_session_watch.py`, and `clawd_status_hub.py` behavior aligned.
- If the hook command changes, rerun `scripts/install_hooks.py`, restart Codex, and trust hooks again.
- Do not make the serial port fixed by default; CH340 auto-detection is intentional.
- Keep Hub as the normal path so dashboard state remains accurate.

For lower-level payload assumptions, read `references/hook-mapping.md`.
