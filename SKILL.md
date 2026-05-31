---
name: codex-clawd-status
description: Automatically use this skill whenever the user mentions Codex status lights, Clawd, Clawd Mochi Tank, hardware status indicators, Codex hooks, hook activation, permission/waiting/working/failed status display, or debugging Codex-to-device status updates. Install and maintain Codex lifecycle hooks that translate Codex session, tool, permission, compact, stop, and subagent events into animation commands for a Clawd Mochi Tank ESP32 display.
---

# Codex Clawd Status

Use this skill to wire Codex hook events to the ESP32 Clawd Mochi Tank firmware.

## Quick Start

1. Ensure the ESP32 is running the Clawd Mochi Tank firmware and is reachable by HTTP, USB serial, or BLE UART.
2. Run `scripts/install_hooks.py` to install Codex hooks into `~/.codex/hooks.json`.
3. Restart active Codex sessions, then run `/hooks` in the Codex CLI to review and trust the new hook definitions.
4. Test manually:
   ```powershell
   python skills/codex-clawd-status/scripts/codex_clawd_hook.py --test typing
   ```

Device-side behavior:

```text
startup / no Bluetooth client -> beacon
Bluetooth serial client opens -> idle
Codex hook command received   -> mapped tank animation
Bluetooth serial disconnects  -> beacon
```

The ESP32 returns a JSON state line after serial/Bluetooth serial commands; the hook records it in `~/.clawd-mochi/status-hook.log`.

Check whether the skill can find the device by itself:

```powershell
python skills/codex-clawd-status/scripts/codex_clawd_hook.py --doctor
```

Default transport is fallback mode: serial/Bluetooth COM first, then HTTP. Set `CLAWD_TANK_URL` to override the default HTTP device URL:

```powershell
$env:CLAWD_TANK_URL="http://192.168.4.1"
```

Use serial only:

```powershell
$env:CLAWD_TANK_TRANSPORT="serial"
$env:CLAWD_TANK_SERIAL_PORT="COM5"
python skills/codex-clawd-status/scripts/codex_clawd_hook.py --test typing
```

Use Bluetooth serial instead:

```powershell
$env:CLAWD_TANK_TRANSPORT="bluetooth"
$env:CLAWD_TANK_SERIAL_PORT="COM5"
python skills/codex-clawd-status/scripts/codex_clawd_hook.py --test typing
```

The current ESP32 firmware advertises as Windows-visible classic Bluetooth SPP. Pair `Claude-Mochi-Tank` in Windows Bluetooth settings first, then use the outgoing COM port shown in Windows. Serial/Bluetooth serial requires `pyserial`.

If `CLAWD_TANK_SERIAL_PORT` is not set, the hook tries to auto-detect a CH340 USB serial adapter first, then a Windows COM port whose device name contains `Claude-Mochi-Tank`.

Send through every channel instead of stopping after the first success:

```powershell
$env:CLAWD_TANK_TRANSPORT="parallel"
```

Accepted transport values:

```text
auto        serial/Bluetooth COM -> HTTP fallback, this is the default
parallel    send by serial, BLE, and HTTP
bluetooth   Bluetooth serial only, alias of serial
serial      serial only
ble         BLE GATT only, for firmware that uses Nordic UART Service
http        HTTP only
serial,http custom ordered fallback list
```

## Event Mapping

Default mapping:

| Codex event | Display animation |
| --- | --- |
| `SessionStart` | `idle` |
| `UserPromptSubmit` | `thinking` |
| `PreToolUse` with `Bash`, `Shell`, `PowerShell` | `building` |
| `PreToolUse` with `apply_patch`, `Edit`, `Write`, `MultiEdit`, `NotebookEdit` | `typing` |
| `PreToolUse` with `Read`, `Grep`, `Glob`, `LS` | `debugger` |
| `PreToolUse` with MCP/LSP-like names | `beacon` |
| `PermissionRequest` | `confused` |
| `PostToolUse` | `thinking` |
| `PreCompact` | `sweeping` |
| `PostCompact` | `thinking` |
| `Stop` | `happy`, then timed `idle` and `sleeping` |
| `SubagentStart` | `conducting` |
| `SubagentStop` | `thinking` |

Codex currently only exposes hook events for supported shell, `apply_patch`, and MCP tool calls; web search and some internal tools may not fire `PreToolUse` / `PostToolUse` hooks.

Lifecycle defaults can be customized before starting Codex:

```powershell
$env:CLAWD_TANK_COMPLETE_ANIM="happy"
$env:CLAWD_TANK_IDLE_ANIM="idle"
$env:CLAWD_TANK_SLEEP_ANIM="sleeping"
$env:CLAWD_TANK_COMPLETE_SECONDS="10"
$env:CLAWD_TANK_IDLE_SECONDS="30"
```

## Scripts

- `scripts/codex_clawd_hook.py`: stdin hook handler. Reads Codex hook JSON, maps it to an animation, sends it to the ESP32 by HTTP, serial, or BLE, and exits 0 on best-effort failures.
- `scripts/codex_session_watch.py`: VS Code / Codex Desktop fallback watcher. Tails `~/.codex/sessions/**/*.jsonl` and drives the same animation mapping when `~/.codex/hooks.json` is not invoked.
- `scripts/install_hooks.py`: idempotently writes hook entries to `~/.codex/hooks.json`.

`codex_clawd_hook.py` auto-starts `codex_session_watch.py` on the first real hook
payload unless `CLAWD_TANK_AUTOSTART_WATCHER=0` is set. This only helps hosts
that invoke hooks at least once; hosts that never invoke hooks still need the
watcher started directly.

## Debugging

Logs are written to:

```text
~/.clawd-mochi/status-hook.log
```

If the display does not change:

1. Open `http://192.168.4.1/state` while connected to the ESP32 AP.
2. Run the hook script with `--test idle`, `--test typing`, or `--test happy`.
3. Check that `~/.codex/hooks.json` contains hook entries pointing at `codex_clawd_hook.py`.
4. Restart Codex and run `/hooks` to trust changed hook definitions.

For the full hook payload assumptions and tool mapping details, read `references/hook-mapping.md`.
