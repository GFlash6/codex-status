#!/usr/bin/env python3
"""Codex hook -> Clawd Mochi Tank animation bridge.

Reads a Codex hook payload from stdin, maps it to an animation, and sends
an HTTP request to the ESP32 firmware. Failures are best-effort and exit 0 so
Codex is never interrupted by display/network issues.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_URL = "http://192.168.4.1"
DEFAULT_BAUD = 115200
DEFAULT_BLE_NAME = "Claude-Mochi-Tank"
CH340_VID = 0x1A86  # WCH CH340/CH341 USB-serial adapter
BLE_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
BLE_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
LOG_DIR = Path.home() / ".clawd-mochi"
LOG_PATH = LOG_DIR / "status-hook.log"
LAST_EVENT_PATH = LOG_DIR / "last_event"
WATCH_PID_PATH = LOG_DIR / "session-watch.pid"


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


# Lifecycle animations. These mirror the Claude mapping by default:
# task complete -> idle -> sleeping, with any new activity cancelling the timer.
SESSION_IDLE_ANIM = os.environ.get("CLAWD_TANK_IDLE_ANIM", "idle")
TASK_COMPLETE_ANIM = os.environ.get("CLAWD_TANK_COMPLETE_ANIM", "happy")
SLEEP_ANIM = os.environ.get("CLAWD_TANK_SLEEP_ANIM", "sleeping")
COMPLETE_DURATION_S = env_float("CLAWD_TANK_COMPLETE_SECONDS", 10.0)
IDLE_DURATION_S = env_float("CLAWD_TANK_IDLE_SECONDS", 30.0)

EDIT_TOOLS   = {
    "Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch",
    "functions.apply_patch",
}
DEBUG_TOOLS  = {
    "Read", "Grep", "Glob", "LS", "view_image", "functions.view_image",
    "list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource",
    "functions.list_mcp_resources", "functions.list_mcp_resource_templates",
    "functions.read_mcp_resource",
}
BUILD_TOOLS  = {
    "Bash", "Shell", "PowerShell", "shell_command", "functions.shell_command",
    "js", "mcp__node_repl.js",
}
WEB_TOOLS    = {"WebFetch", "WebSearch", "web.run", "imagegen", "image_gen.imagegen"}
AGENT_TOOLS  = {"Task", "Agent", "Subagent"}
MANAGE_TOOLS = {
    "TodoWrite", "TodoRead", "update_plan", "get_goal", "create_goal", "update_goal",
    "functions.update_plan", "functions.get_goal", "functions.create_goal", "functions.update_goal",
}
ASK_TOOLS    = {"AskUserQuestion", "AskFollowup", "request_user_input", "functions.request_user_input"}
BEACON_HINTS = ("mcp", "lsp", "language", "context")


def log(message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Last-event timestamp (used to cancel timed transitions on new activity)
# ---------------------------------------------------------------------------

def touch_last_event() -> float:
    ts = time.time()
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        LAST_EVENT_PATH.write_text(f"{ts:.6f}", encoding="utf-8")
    except OSError:
        pass
    return ts


def read_last_event() -> float:
    try:
        return float(LAST_EVENT_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return 0.0


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2.0,
                check=False,
            )
            return f'"{pid}"' in (result.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_watcher_pid() -> int:
    try:
        return int(WATCH_PID_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def ensure_session_watcher(args: argparse.Namespace) -> None:
    """Start the session JSONL watcher once, when hook support is available."""
    if os.environ.get("CLAWD_TANK_AUTOSTART_WATCHER", "1").lower() in {"0", "false", "no", "off"}:
        return

    pid = read_watcher_pid()
    if pid_is_running(pid):
        log(f"watch already running pid={pid}")
        return

    watcher = Path(__file__).with_name("codex_session_watch.py")
    if not watcher.exists():
        log(f"watch autostart skipped; missing {watcher}")
        return

    cmd = [sys.executable, str(watcher), "--follow-latest"]
    if args.transport:
        cmd += ["--transport", args.transport]
    if args.port:
        cmd += ["--port", args.port]
    if args.baud is not None:
        cmd += ["--baud", str(args.baud)]
    if args.ble_address:
        cmd += ["--ble-address", args.ble_address]
    if args.ble_name:
        cmd += ["--ble-name", args.ble_name]

    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **kwargs)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        WATCH_PID_PATH.write_text(str(proc.pid), encoding="utf-8")
        log(f"watch autostarted pid={proc.pid}")
    except Exception as exc:
        log(f"watch autostart failed: {exc}")


# ---------------------------------------------------------------------------
# Transport helpers
# ---------------------------------------------------------------------------

def device_url() -> str:
    return os.environ.get("CLAWD_TANK_URL", DEFAULT_URL).rstrip("/")


def transport_name(cli_transport: str | None = None) -> str:
    return (cli_transport or os.environ.get("CLAWD_TANK_TRANSPORT") or "auto").lower()


def transport_list(selected: str) -> list[str]:
    aliases = {
        "all": "parallel",
        "fallback": "auto",
        "bt": "serial",
        "bluetooth": "serial",
    }
    selected = aliases.get(selected, selected)
    if selected == "auto":
        return ["serial", "http"]
    if selected == "parallel":
        return ["serial", "ble", "http"]
    if "," in selected:
        return [item.strip().lower() for item in selected.split(",") if item.strip()]
    return [selected]


def command_payload(anim: str) -> str:
    return json.dumps({"auto": False, "anim": anim}, separators=(",", ":")) + "\n"


def discover_windows_bluetooth_port(device_name: str = DEFAULT_BLE_NAME) -> str | None:
    ports = discover_windows_bluetooth_ports(device_name)
    return ports[0] if ports else None


def discover_windows_bluetooth_ports(device_name: str = DEFAULT_BLE_NAME) -> list[str]:
    if os.name != "nt":
        return []

    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "Get-PnpDevice -Class Ports -PresentOnly | "
        f"Where-Object {{$_.FriendlyName -like '*{device_name}*'}} | "
        "Select-Object -ExpandProperty FriendlyName"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1.5,
            check=False,
        )
    except Exception as exc:
        log(f"bluetooth COM discovery failed: {exc}")
        return []

    return re.findall(r"\((COM\d+)\)", result.stdout)


def discover_ch340_port() -> str | None:
    """Return the first serial port whose USB VID matches CH340 (0x1A86)."""
    try:
        from serial.tools import list_ports  # type: ignore
        for info in list_ports.comports():
            if info.vid == CH340_VID:
                return info.device
        # Fallback: match by description string for systems where VID is unavailable
        for info in list_ports.comports():
            if "CH340" in (info.description or "") or "CH341" in (info.description or ""):
                return info.device
    except Exception:
        pass
    return None


def list_windows_ports() -> list[str]:
    if os.name != "nt":
        return []
    script = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-PnpDevice -Class Ports -PresentOnly | Select-Object -ExpandProperty FriendlyName"
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1.5,
            check=False,
        )
    except Exception:
        return []
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def doctor() -> int:
    print("Clawd transport doctor")
    print(f"default transport: {transport_name()}")
    print(f"http url: {device_url()}")

    ch340 = discover_ch340_port()
    print(f"CH340 auto-detect: {ch340 or 'none'}")

    ports = discover_windows_bluetooth_ports()
    if ports:
        print(f"bluetooth COM candidates for {DEFAULT_BLE_NAME}: {', '.join(ports)}")
    else:
        print(f"bluetooth COM candidates for {DEFAULT_BLE_NAME}: none")
        all_ports = list_windows_ports()
        if all_ports:
            print("present Windows serial ports:")
            for item in all_ports:
                print(f"  {item}")

    try:
        import serial  # noqa: F401  # type: ignore
        print("pyserial: installed")
    except ImportError:
        print("pyserial: missing; install with: python -m pip install pyserial")

    try:
        import bleak  # noqa: F401  # type: ignore
        print("bleak: installed")
    except ImportError:
        print("bleak: missing; only needed for BLE GATT mode")

    return 0


# ---------------------------------------------------------------------------
# Send helpers
# ---------------------------------------------------------------------------

def send_anim_http(anim: str) -> bool:
    base = device_url()
    # Disable firmware auto-cycle before explicit Codex-driven status.
    for path in ("/auto?on=0", f"/anim?id={urllib.parse.quote(anim)}"):
        try:
            with urllib.request.urlopen(base + path, timeout=0.8) as resp:
                resp.read(256)
        except Exception as exc:  # best effort only
            log(f"send failed anim={anim} path={path}: {exc}")
            return False
    log(f"sent http anim={anim}")
    return True


def send_anim_serial(anim: str, port: str | None = None, baud: int | None = None) -> bool:
    serial_port = (
        port
        or os.environ.get("CLAWD_TANK_SERIAL_PORT")
        or discover_ch340_port()
        or discover_windows_bluetooth_port()
    )
    if not serial_port:
        log("serial transport selected but no COM port was found; set CLAWD_TANK_SERIAL_PORT/--port")
        return False

    try:
        import serial  # type: ignore
    except ImportError:
        log("serial transport requires pyserial: python -m pip install pyserial")
        return False

    try:
        ser = serial.Serial()
        ser.port = serial_port
        ser.baudrate = baud or int(os.environ.get("CLAWD_TANK_SERIAL_BAUD", DEFAULT_BAUD))
        ser.timeout = 0.4
        ser.rtscts = False
        ser.dsrdtr = False
        ser.dtr = False
        ser.rts = False
        with ser:
            ser.dtr = False
            ser.rts = False
            ser.write(command_payload(anim).encode("utf-8"))
            ser.flush()
            deadline = time.time() + 0.8
            while time.time() < deadline:
                line = ser.readline().decode("utf-8", errors="replace").strip()
                if line.startswith("{") and "\"anim\"" in line:
                    log(f"serial state port={serial_port}: {line}")
                    break
        log(f"sent serial port={serial_port} anim={anim}")
        return True
    except Exception as exc:
        log(f"serial send failed port={serial_port} anim={anim}: {exc}")
        return False


async def _send_anim_ble_async(anim: str, address: str | None, name: str) -> bool:
    try:
        from bleak import BleakClient, BleakScanner  # type: ignore
    except ImportError:
        log("ble transport requires bleak: python -m pip install bleak")
        return False

    target = address
    if not target:
        devices = await BleakScanner.discover(timeout=2.5)
        for device in devices:
            if (device.name or "").startswith(name):
                target = device.address
                break

    if not target:
        log(f"ble device not found name={name!r}")
        return False

    try:
        async with BleakClient(target, timeout=4.0) as client:
            await client.write_gatt_char(BLE_RX_UUID, command_payload(anim).encode("utf-8"), response=False)
        log(f"sent ble target={target} anim={anim}")
        return True
    except Exception as exc:
        log(f"ble send failed target={target} anim={anim}: {exc}")
        return False


def send_anim_ble(anim: str, address: str | None = None, name: str | None = None) -> bool:
    return asyncio.run(
        _send_anim_ble_async(
            anim,
            address or os.environ.get("CLAWD_TANK_BLE_ADDRESS"),
            name or os.environ.get("CLAWD_TANK_BLE_NAME", DEFAULT_BLE_NAME),
        )
    )


def send_anim(anim: str, transport: str | None = None, port: str | None = None, baud: int | None = None,
              ble_address: str | None = None, ble_name: str | None = None) -> None:
    selected = transport_name(transport)
    transports = transport_list(selected)
    sent_any = False
    parallel = selected in ("parallel", "all")

    for item in transports:
        sent = False
        if item == "http":
            sent = send_anim_http(anim)
        elif item == "serial":
            sent = send_anim_serial(anim, port=port, baud=baud)
        elif item == "ble":
            sent = send_anim_ble(anim, address=ble_address, name=ble_name)
        else:
            log(f"unknown transport={item!r}; expected http, serial, ble, auto, parallel, or comma list")
            continue

        sent_any = sent_any or sent
        if sent and not parallel:
            break

    if not sent_any:
        log(f"no transport delivered anim={anim}")


# ---------------------------------------------------------------------------
# Timed completion → idle → sleeping transition (spawned after Stop)
# ---------------------------------------------------------------------------

def spawn_timed_transition(event_time: float, args: argparse.Namespace) -> None:
    """Detach a background process: complete -> idle -> sleeping."""
    cmd = [sys.executable, __file__, "--timed-transition", f"{event_time:.6f}"]
    if args.transport:
        cmd += ["--transport", args.transport]
    if args.port:
        cmd += ["--port", args.port]
    if args.baud is not None:
        cmd += ["--baud", str(args.baud)]
    if args.ble_address:
        cmd += ["--ble-address", args.ble_address]
    if args.ble_name:
        cmd += ["--ble-name", args.ble_name]

    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen(cmd, **kwargs)
    except Exception as exc:
        log(f"failed to spawn timed transition: {exc}")


def run_timed_transition(event_time: float, transport: str | None, port: str | None,
                         baud: int | None, ble_address: str | None, ble_name: str | None) -> None:
    """Background: wait after completion, then idle, then sleeping."""
    kw = dict(transport=transport, port=port, baud=baud, ble_address=ble_address, ble_name=ble_name)

    time.sleep(COMPLETE_DURATION_S)
    if read_last_event() > event_time + 1.0:
        log(f"timed transition aborted (new activity)")
        return
    send_anim(SESSION_IDLE_ANIM, **kw)
    log(f"timed transition: {TASK_COMPLETE_ANIM} -> {SESSION_IDLE_ANIM}")

    time.sleep(IDLE_DURATION_S)
    if read_last_event() > event_time + 1.0:
        log(f"timed transition aborted (new activity during idle)")
        return
    send_anim(SLEEP_ANIM, **kw)
    log(f"timed transition: {SESSION_IDLE_ANIM} -> {SLEEP_ANIM}")


# ---------------------------------------------------------------------------
# Animation mapping
# ---------------------------------------------------------------------------

def normalize_tool_name(tool_name: str) -> str:
    """Collapse Codex namespaced tool ids to their stable leaf names."""
    tool = str(tool_name or "").strip()
    if not tool:
        return ""
    return tool.rsplit(".", 1)[-1]


def tool_to_anim(tool_name: str, tool_input: object | None = None) -> str:
    tool = normalize_tool_name(tool_name)
    raw_tool = str(tool_name or "")
    candidates = {tool, raw_tool}

    if tool == "parallel" and isinstance(tool_input, dict):
        recipients = [
            str(item.get("recipient_name", ""))
            for item in tool_input.get("tool_uses", [])
            if isinstance(item, dict)
        ]
        if recipients and all(tool_matches(name, EDIT_TOOLS) for name in recipients):
            return "typing"
        if recipients and all(tool_matches(name, BUILD_TOOLS) for name in recipients):
            return "building"
        if recipients and all(tool_matches(name, DEBUG_TOOLS) for name in recipients):
            return "debugger"
        return "juggling"

    if candidates & EDIT_TOOLS:
        return "typing"
    if candidates & DEBUG_TOOLS:
        return "debugger"
    if candidates & BUILD_TOOLS:
        return "building"
    if candidates & WEB_TOOLS:
        return "wizard"
    if candidates & AGENT_TOOLS:
        return "conducting"
    if candidates & MANAGE_TOOLS:
        return "juggling"
    if candidates & ASK_TOOLS:
        return "confused"

    lower = raw_tool.lower()
    if any(hint in lower for hint in BEACON_HINTS):
        return "beacon"
    return "typing"


def tool_matches(tool_name: str, names: set[str]) -> bool:
    return bool({normalize_tool_name(tool_name), str(tool_name or "")} & names)


def payload_to_anim(payload: dict) -> str | None:
    event = payload.get("hook_event_name") or payload.get("event") or ""
    tool = payload.get("tool_name") or payload.get("toolName") or ""
    tool_input = payload.get("tool_input") or payload.get("toolInput")

    if event == "SessionStart":
        return SESSION_IDLE_ANIM
    if event == "PreToolUse":
        return tool_to_anim(str(tool), tool_input)
    if event == "PermissionRequest":
        return "confused"
    if event == "PostToolUse":
        # Once the tool has completed, Codex is back to reading the result and
        # deciding the next step. Keep the display on the model state instead
        # of leaving it stuck on the previous tool animation.
        return "thinking"
    if event == "PreCompact":
        return "sweeping"
    if event == "PostCompact":
        return "thinking"
    if event == "Stop":
        return TASK_COMPLETE_ANIM
    if event == "UserPromptSubmit":
        return "thinking"
    if event == "SubagentStart":
        return "conducting"
    if event == "SubagentStop":
        return "thinking"

    return None


def read_payload() -> dict | None:
    raw = sys.stdin.read()
    if not raw.strip():
        return None
    if os.environ.get("CLAWD_DEBUG"):
        log(f"raw payload: {raw.strip()}")
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError as exc:
        log(f"invalid json: {exc}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", help="send a specific animation and exit")
    parser.add_argument("--doctor", action="store_true", help="show discovered transports and dependencies")
    parser.add_argument("--print-mapping", action="store_true")
    parser.add_argument("--transport", help="http, serial, ble, auto, parallel/all, or comma list")
    parser.add_argument("--port", help="serial port, for example COM5")
    parser.add_argument("--baud", type=int, default=None)
    parser.add_argument("--ble-address")
    parser.add_argument("--ble-name", default=None)
    parser.add_argument("--timed-transition", type=float, default=None, metavar="EPOCH",
                        help="internal: run complete->idle->sleeping timer started at EPOCH")
    args = parser.parse_args()

    if args.doctor:
        return doctor()

    if args.print_mapping:
        all_named = EDIT_TOOLS | DEBUG_TOOLS | BUILD_TOOLS | WEB_TOOLS | AGENT_TOOLS | MANAGE_TOOLS | ASK_TOOLS
        for name in sorted(all_named):
            print(f"{name}: {tool_to_anim(name)}")
        return 0

    # Background timed-transition mode (spawned by Stop handler)
    if args.timed_transition is not None:
        run_timed_transition(
            args.timed_transition,
            args.transport, args.port, args.baud, args.ble_address, args.ble_name,
        )
        return 0

    if args.test:
        send_anim(
            args.test,
            transport=args.transport,
            port=args.port,
            baud=args.baud,
            ble_address=args.ble_address,
            ble_name=args.ble_name,
        )
        return 0

    payload = read_payload()
    if payload is None:
        return 0

    ensure_session_watcher(args)

    # Update activity timestamp so timed transitions can detect new events
    event_time = touch_last_event()

    anim = payload_to_anim(payload)
    if anim:
        event = payload.get("hook_event_name") or payload.get("event") or ""
        tool = payload.get("tool_name") or payload.get("toolName") or ""
        log(f"mapped event={event!r} tool={tool!r} anim={anim}")
        send_anim(
            anim,
            transport=args.transport,
            port=args.port,
            baud=args.baud,
            ble_address=args.ble_address,
            ble_name=args.ble_name,
        )
        # After Stop -> completion animation, spawn timer for idle then sleeping.
        if event == "Stop":
            spawn_timed_transition(event_time, args)
    else:
        ntype = payload.get("notification_type") or payload.get("type") or ""
        log(f"ignored payload event={payload.get('hook_event_name')!r} notification_type={ntype!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
