# Codex Clawd Status - Hook Animation Mapping

This directory is the Codex-flavored version of `claude-clawd-status`. It keeps the same Clawd Mochi Tank transport code, but installs Codex hooks into `~/.codex/hooks.json` and maps Codex hook events.

## Lifecycle

| Moment | Animation | Meaning |
| --- | --- | --- |
| `SessionStart` | `idle` | Codex session is ready and waiting |
| `UserPromptSubmit` | `thinking` | Codex has received your prompt and is reasoning |
| `PreToolUse` | tool-specific | Codex is about to run a supported tool |
| `PermissionRequest` | `confused` | Codex is waiting for approval |
| `PostToolUse` | `thinking` | The tool finished; Codex is reading the result |
| `PreCompact` | `sweeping` | Codex is compacting context |
| `PostCompact` | `thinking` | Compact finished and Codex is processing again |
| `Stop` | `happy` -> `idle` -> `sleeping` | Turn finished, then idle timer takes over |

## Tool Types

| Tool | Animation | Meaning |
| --- | --- | --- |
| `apply_patch`, `functions.apply_patch`, `Edit`, `Write`, `MultiEdit`, `NotebookEdit` | `typing` | Editing files |
| `Read`, `Grep`, `Glob`, `LS`, `functions.view_image`, MCP resource reads | `debugger` | Reading, inspecting, or searching files/resources |
| `Bash`, `Shell`, `PowerShell`, `functions.shell_command`, `mcp__node_repl.js` | `building` | Running commands or executable code |
| `WebFetch`, `WebSearch`, `web.run`, `image_gen.imagegen` | `wizard` | Web lookup or generated media |
| `Task`, `Agent`, `Subagent` | `conducting` | Delegating work |
| `TodoWrite`, `TodoRead`, `functions.update_plan`, goal tools | `juggling` | Managing tasks |
| `AskUserQuestion`, `AskFollowup`, `functions.request_user_input` | `confused` | Waiting for user input |
| `mcp__*`, `lsp`, `language`, `context` names | `beacon` | External service or MCP call |
| Unknown tool | `typing` | Conservative fallback |

## Install

```powershell
python skills/codex-clawd-status/scripts/install_hooks.py
```

Then restart Codex and run `/hooks` to review and trust the hook definitions. Codex may require trust again whenever the hook command changes.

## Test

```powershell
python skills/codex-clawd-status/scripts/codex_clawd_hook.py --doctor
python skills/codex-clawd-status/scripts/codex_clawd_hook.py --test typing
python skills/codex-clawd-status/scripts/codex_clawd_hook.py --print-mapping
```

## VS Code Realtime Fallback

Some VS Code or Codex Desktop sessions write tool activity to
`~/.codex/sessions/**/*.jsonl` without invoking `~/.codex/hooks.json`. In that
case, run the session watcher:

```powershell
Start-Process -FilePath "C:\Python314\python.exe" `
  -ArgumentList @(
    "skills/codex-clawd-status/scripts/codex_session_watch.py",
    "--follow-latest",
    "--transport", "serial",
    "--port", "COM5"
  ) `
  -WindowStyle Hidden
```

The watcher uses the same animation mapping and transport code as the hook
script, but reads Codex's session event log directly. Omit `--port` to use
auto-detected CH340 serial.

`codex_clawd_hook.py` also auto-starts this watcher the first time a real hook
payload arrives. Disable that behavior with:

```powershell
$env:CLAWD_TANK_AUTOSTART_WATCHER = "0"
```

If a host never invokes `~/.codex/hooks.json`, it cannot trigger this autostart;
start the watcher directly in that case.

## Transport Configuration

Default priority is CH340 USB serial, then Bluetooth serial, then HTTP `192.168.4.1`.

```powershell
$env:CLAWD_TANK_SERIAL_PORT = "COM3"
$env:CLAWD_TANK_TRANSPORT = "serial"
$env:CLAWD_DEBUG = "1"
```

Supported transport values:

```text
auto, parallel, bluetooth, serial, ble, http, serial,http
```

## Lifecycle Configuration

Defaults match the Claude status behavior: completion is `happy`, idle is `idle`,
and long idle becomes `sleeping`.

```powershell
$env:CLAWD_TANK_COMPLETE_ANIM = "happy"
$env:CLAWD_TANK_IDLE_ANIM = "idle"
$env:CLAWD_TANK_SLEEP_ANIM = "sleeping"
$env:CLAWD_TANK_COMPLETE_SECONDS = "10"
$env:CLAWD_TANK_IDLE_SECONDS = "30"
```
