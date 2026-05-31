# Hook Mapping Reference

The hook script accepts one JSON object on stdin. It expects Codex hook fields and keeps defensive fallbacks for nearby Claude-style names where harmless.

Common Codex fields:

- `hook_event_name`: event name such as `PreToolUse`, `PermissionRequest`, `PostToolUse`, or `Stop`
- `session_id`: current Codex session identifier
- `turn_id`: current Codex turn identifier for turn-scoped hooks
- `tool_name`: canonical tool name for tool events
- `tool_input`: tool-specific input, such as a Bash command or `apply_patch` command
- `cwd`: current project path
- `model`: active Codex model slug
- `permission_mode`: `default`, `acceptEdits`, `plan`, `dontAsk`, or `bypassPermissions`
- `agent_id` / `agent_type`: subagent identifiers for subagent hooks

Animation endpoint:

```text
GET http://192.168.4.1/anim?id=<animation>
```

Supported firmware animations:

```text
idle, typing, thinking, building, juggling, conducting, debugger, wizard,
beacon, confused, sweeping, walking, going_away, alert, happy, sleeping,
dizzy, disconnected
```

## Event to Animation Mapping

| Codex event | Animation | Notes |
| --- | --- | --- |
| `SessionStart` | `idle` | Session registered and waiting for work; configurable with `CLAWD_TANK_IDLE_ANIM` |
| `UserPromptSubmit` | `thinking` | Codex starts reasoning before tools |
| `PreToolUse` | tool-specific | Show the supported tool that is about to run |
| `PermissionRequest` | `confused` | Codex is waiting for approval |
| `PostToolUse` | `thinking` | Tool finished; Codex is reading results and deciding the next step |
| `PreCompact` | `sweeping` | Context compaction is about to start |
| `PostCompact` | `thinking` | Compaction finished; Codex resumes processing |
| `SubagentStart` | `conducting` | Subagent spawned; overrides current tool |
| `SubagentStop` | `thinking` | Subagent done, back to processing |
| `Stop` | `happy` | Codex finished the turn; configurable with `CLAWD_TANK_COMPLETE_ANIM`, then timed `idle` and `sleeping` |

## Tool to Animation Mapping

The animation semantics follow the Claude mapping, but Codex tool names are often
namespaced. The hook checks both the full Codex name and the final leaf name, so
`functions.shell_command` and `shell_command` resolve the same way.

| Tool category | Tools | Animation |
| --- | --- | --- |
| Edit | Claude: `Edit`, `Write`, `MultiEdit`, `NotebookEdit`; Codex: `apply_patch`, `functions.apply_patch` | `typing` |
| Debug / read / inspect | Claude: `Read`, `Grep`, `Glob`, `LS`; Codex: `view_image`, `functions.view_image`, `list_mcp_resources`, `list_mcp_resource_templates`, `read_mcp_resource` | `debugger` |
| Build / shell / execution | Claude: `Bash`, `Shell`, `PowerShell`; Codex: `shell_command`, `functions.shell_command`, `js`, `mcp__node_repl.js` | `building` |
| Web / generated media | Claude: `WebFetch`, `WebSearch`; Codex: `web.run`, `imagegen`, `image_gen.imagegen` | `wizard` |
| Agent | `Task`, `Agent`, `Subagent` | `conducting` |
| Task / goal management | Claude: `TodoWrite`, `TodoRead`; Codex: `update_plan`, `get_goal`, `create_goal`, `update_goal` | `juggling` |
| Ask user | Claude: `AskUserQuestion`, `AskFollowup`; Codex: `request_user_input` | `confused` |
| MCP / LSP name hint | names containing `mcp`, `lsp`, `language`, or `context` | `beacon` |
| Parallel tool wrapper | `multi_tool_use.parallel` | nested tools if unambiguous, otherwise `juggling` |
| Unknown | anything else | `typing` |

Codex currently exposes `PreToolUse`, `PermissionRequest`, and `PostToolUse` for supported shell, `apply_patch`, and MCP calls. Some tool names in the table are retained as compatibility aliases for payloads from adjacent hosts or future Codex releases.

## Unused Animations

| Animation | Reason |
| --- | --- |
| `alert` | Available through `CLAWD_TANK_COMPLETE_ANIM=alert`, but default completion is `happy` |
| `dizzy` | Codex does not currently expose a `StopFailure` hook equivalent |
| `going_away` | Codex does not currently expose a `SessionEnd` hook equivalent |
| `walking` | Placeholder sprite; reserved for future multi-session transitions |
| `disconnected` | Firmware-autonomous on BLE drop; not sent by hook |

## Timed Stop Behavior

```text
Stop -> happy (10s) -> idle (30s) -> sleeping
             -> any new event cancels the pending timer
```

Lifecycle defaults can be customized before Codex starts:

```powershell
$env:CLAWD_TANK_COMPLETE_ANIM = "happy"
$env:CLAWD_TANK_IDLE_ANIM = "idle"
$env:CLAWD_TANK_SLEEP_ANIM = "sleeping"
$env:CLAWD_TANK_COMPLETE_SECONDS = "10"
$env:CLAWD_TANK_IDLE_SECONDS = "30"
```

The script turns off ESP auto-cycle (`/auto?on=0`) before sending explicit status so manual state is not overridden by the firmware's own cycling.

## VS Code Session Watcher

If a session has `originator: codex_vscode` or `originator: Codex Desktop`, it
may record `response_item` tool events in `~/.codex/sessions/**/*.jsonl`
without invoking `~/.codex/hooks.json`. `scripts/codex_session_watch.py` tails
the newest JSONL stream, switches as newer sessions appear, and maps:

| Session JSONL event | Animation |
| --- | --- |
| `event_msg` `user_message` | `thinking` |
| `response_item` `function_call` / `custom_tool_call` | tool-specific |
| `response_item` `function_call_output` / `custom_tool_call_output` | `thinking` |
| `event_msg` `task_complete` | completion animation, then timed idle/sleeping |

`codex_clawd_hook.py` will auto-start this watcher after the first real hook
payload unless `CLAWD_TANK_AUTOSTART_WATCHER=0` is set. This is a bootstrap path
for hosts that invoke hooks sometimes; a host that never invokes hooks cannot use
hook-based autostart and needs the watcher launched directly.
