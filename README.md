# Codex Clawd Status

Codex status bridge for the Clawd Mochi Tank ESP32 display.

Full install, usage, Hub, transport, watcher, and troubleshooting documentation lives in:

```text
SKILL.md
```

## Runtime Flow

```text
Codex native hooks and/or Codex session JSONL watcher
  -> codex_clawd_hook.py / codex_session_watch.py
  -> Hook Hub at http://127.0.0.1:8765
  -> BLE / ESP32 serial
  -> ESP32
```

## Install

```powershell
C:\Python314\python.exe C:\Users\admin\.codex\skills\codex-clawd-status\scripts\install_hooks.py
```

Then restart Codex and run `/hooks` to review and trust hook definitions.

## Daily Start

Start the background UI controller:

```powershell
Start-Process -FilePath "C:\Python314\python.exe" `
  -ArgumentList @(
    "C:\Users\admin\.codex\skills\codex-clawd-status\scripts\clawd_hub_app.py",
    "--minimized"
  ) `
  -WindowStyle Hidden
```

The UI controller keeps Hub and the Codex watcher alive, provides restart
buttons, and opens the dashboard. If `pystray` is installed it lives in the
system tray; otherwise it uses a small Tkinter window.

Start Hub:

```powershell
Start-Process -FilePath "C:\Python314\python.exe" `
  -ArgumentList @(
    "C:\Users\admin\.codex\skills\codex-clawd-status\scripts\clawd_status_hub.py",
    "--transport", "auto"
  ) `
  -WindowStyle Hidden
```

Start watcher for VS Code / Desktop sessions:

```powershell
Start-Process -FilePath "C:\Python314\python.exe" `
  -ArgumentList @(
    "C:\Users\admin\.codex\skills\codex-clawd-status\scripts\codex_session_watch.py",
    "--follow-latest"
  ) `
  -WindowStyle Hidden
```

Open:

```text
http://127.0.0.1:8765
```

## Client IDs

```text
codex-code      native ~/.codex/hooks.json events
codex-vscode    session watcher, originator=codex_vscode
codex-desktop   session watcher, originator=Codex Desktop
codex-watch     session watcher fallback
manual          Hub dashboard buttons
```

## Test

```powershell
C:\Python314\python.exe C:\Users\admin\.codex\skills\codex-clawd-status\scripts\codex_clawd_hook.py --doctor
C:\Python314\python.exe C:\Users\admin\.codex\skills\codex-clawd-status\scripts\codex_clawd_hook.py --test thinking
Invoke-RestMethod http://127.0.0.1:8765/state
```

## Notes

- Default transport is `auto`: BLE, then auto-detected ESP32 serial.
- Serial ports are not fixed; CH340/CH341 adapters and native ESP32 USB CDC/JTAG ports are detected from port metadata.
- Hub localhost requests bypass system proxy settings.
- Detailed behavior and troubleshooting are in `SKILL.md`.
