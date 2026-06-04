#!/usr/bin/env python3
"""Codex hook -> Clawd Mochi Tank animation bridge.

Reads a Codex hook payload from stdin, maps it to an animation, and sends
it to the ESP32 firmware by BLE or serial. Failures are best-effort and exit 0
so Codex is never interrupted by display/network issues.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path


DEFAULT_BAUD = 115200
DEFAULT_BLE_NAME = "Claude-Mochi-Tank"
CH340_VID = 0x1A86  # WCH CH340/CH341 USB-serial adapter
ESPRESSIF_USB_VID = 0x303A
BLE_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
BLE_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
LOG_DIR = Path.home() / ".clawd-mochi"
LOG_PATH = LOG_DIR / "status-hook.log"
LAST_EVENT_PATH = LOG_DIR / "last_event"
WATCH_PID_PATH = LOG_DIR / "session-watch.pid"
HUB_PID_PATH = LOG_DIR / "status-hub.pid"
WATCH_START_LOCK_PATH = LOG_DIR / "session-watch.start.lock"
HUB_START_LOCK_PATH = LOG_DIR / "status-hub.start.lock"
DEFAULT_HUB_URL = "http://127.0.0.1:8765"
DEFAULT_CLIENT_ID = "codex-code"
NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SERIAL_LOCK = threading.Lock()


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


def read_hub_pid() -> int:
    try:
        return int(HUB_PID_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def acquire_start_lock(path: Path, stale_seconds: float = 10.0) -> bool:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()} {time.time():.6f}\n")
        return True
    except FileExistsError:
        try:
            if time.time() - path.stat().st_mtime > stale_seconds:
                path.unlink(missing_ok=True)
                return acquire_start_lock(path, stale_seconds)
        except OSError:
            pass
        return False
    except OSError:
        return False


def release_start_lock(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def ensure_session_watcher(args: argparse.Namespace) -> None:
    """Start the session JSONL watcher once, when hook support is available."""
    if os.environ.get("CLAWD_TANK_AUTOSTART_WATCHER", "1").lower() in {"0", "false", "no", "off"}:
        return

    pid = read_watcher_pid()
    if pid_is_running(pid):
        log(f"watch already running pid={pid}")
        return
    if not acquire_start_lock(WATCH_START_LOCK_PATH):
        time.sleep(0.4)
        if pid_is_running(read_watcher_pid()):
            log("watch autostart skipped; another starter won")
            return
        log("watch autostart skipped; start lock is active")
        return

    try:
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
    finally:
        release_start_lock(WATCH_START_LOCK_PATH)


def hub_url(cli_url: str | None = None) -> str:
    return (cli_url or os.environ.get("CLAWD_TANK_HUB_URL") or DEFAULT_HUB_URL).rstrip("/")


def hub_urlopen(req: str | urllib.request.Request, timeout: float):
    return NO_PROXY_OPENER.open(req, timeout=timeout)


def client_id(cli_client_id: str | None = None) -> str:
    return (cli_client_id or os.environ.get("CLAWD_TANK_CLIENT_ID") or DEFAULT_CLIENT_ID).strip()


def hub_enabled(args: argparse.Namespace | None = None) -> bool:
    if args is not None and getattr(args, "no_hub", False):
        return False
    return os.environ.get("CLAWD_TANK_USE_HUB", "1").lower() not in {"0", "false", "no", "off"}


def hub_required(args: argparse.Namespace | None = None) -> bool:
    if args is not None and getattr(args, "hub_required", False):
        return True
    return os.environ.get("CLAWD_TANK_HUB_REQUIRED", "1").lower() not in {"0", "false", "no", "off"}


def ensure_hub(args: argparse.Namespace) -> None:
    if not hub_enabled(args):
        return
    if ping_hub(args.hub_url):
        return
    pid = read_hub_pid()
    if pid_is_running(pid):
        return
    if not acquire_start_lock(HUB_START_LOCK_PATH):
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if ping_hub(args.hub_url):
                return
            time.sleep(0.15)
        log("hub autostart skipped; start lock is active")
        return

    try:
        if ping_hub(args.hub_url):
            return
        hub = Path(__file__).with_name("clawd_status_hub.py")
        if not hub.exists():
            log(f"hub autostart skipped; missing {hub}")
            return

        cmd = [sys.executable, str(hub)]
        if args.transport:
            cmd += ["--transport", args.transport]
        if args.port:
            cmd += ["--serial-port", args.port]
        if args.baud is not None:
            cmd += ["--baud", str(args.baud)]
        if args.ble_address:
            cmd += ["--ble-address", args.ble_address]
        if args.ble_name:
            cmd += ["--ble-name", args.ble_name]

        kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x08000000
        else:
            kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(cmd, **kwargs)
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            HUB_PID_PATH.write_text(str(proc.pid), encoding="utf-8")
            log(f"hub autostarted pid={proc.pid}")
        except Exception as exc:
            log(f"hub autostart failed: {exc}")
            return

        deadline = time.time() + 3.0
        while time.time() < deadline:
            if ping_hub(args.hub_url):
                return
            time.sleep(0.15)
    finally:
        release_start_lock(HUB_START_LOCK_PATH)


def ping_hub(cli_url: str | None = None) -> bool:
    try:
        with hub_urlopen(hub_url(cli_url) + "/health", timeout=0.4) as resp:
            return resp.status == 200
    except Exception:
        return False


def send_anim_hub(anim: str, args: argparse.Namespace, payload: dict | None = None, event_time: float | None = None) -> bool:
    ensure_hub(args)
    event = payload.get("hook_event_name") or payload.get("event") if payload else ""
    tool = payload.get("tool_name") or payload.get("toolName") if payload else ""
    body = json.dumps(
        {
            "source": "codex",
            "client_id": client_id(getattr(args, "client_id", None)),
            "client_kind": "codex",
            "anim": anim,
            "event": event,
            "tool": tool,
            "payload": payload or {},
            "event_time": event_time,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    req = urllib.request.Request(
        hub_url(args.hub_url) + "/hook",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with hub_urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            ok = bool(data.get("ok"))
            log(f"hub delivery anim={anim} ok={ok} status={data.get('status')}")
            return ok
    except Exception as exc:
        log(f"hub delivery failed anim={anim}: {exc}")
        return False


def deliver_anim(anim: str, args: argparse.Namespace, payload: dict | None = None,
                 event_time: float | None = None) -> bool:
    if hub_enabled(args):
        if send_anim_hub(anim, args, payload=payload, event_time=event_time):
            return True
        if hub_required(args):
            return False
    send_anim(
        anim,
        transport=args.transport,
        port=args.port,
        baud=args.baud,
        ble_address=args.ble_address,
        ble_name=args.ble_name,
    )
    return True


# ---------------------------------------------------------------------------
# Transport helpers
# ---------------------------------------------------------------------------

def transport_name(cli_transport: str | None = None) -> str:
    return (cli_transport or os.environ.get("CLAWD_TANK_TRANSPORT") or "auto").lower()


def transport_list(selected: str) -> list[str]:
    aliases = {
        "all": "parallel",
        "fallback": "auto",
        "bt": "ble",
        "bluetooth": "ble",
    }
    selected = aliases.get(selected, selected)
    if selected == "auto":
        return ["ble", "serial"]
    if selected == "parallel":
        return ["ble", "serial"]
    if "," in selected:
        return [item.strip().lower() for item in selected.split(",") if item.strip() and item.strip().lower() != "http"]
    return [selected]


def command_payload(anim: str) -> str:
    return json.dumps({"auto": False, "anim": anim}, separators=(",", ":")) + "\n"


def port_matches_ch340(info: object) -> bool:
    """Match CH340/CH341 across pyserial's available metadata fields."""
    if getattr(info, "vid", None) == CH340_VID:
        return True
    fields = (
        "device",
        "name",
        "description",
        "hwid",
        "manufacturer",
        "product",
        "interface",
        "location",
    )
    haystack = " ".join(str(getattr(info, field, "") or "") for field in fields).upper()
    return any(token in haystack for token in ("CH340", "CH341", "1A86", "USB-SERIAL"))


def port_score(info: object) -> int:
    fields = (
        "device",
        "name",
        "description",
        "hwid",
        "manufacturer",
        "product",
        "interface",
        "location",
    )
    haystack = " ".join(str(getattr(info, field, "") or "") for field in fields).upper()
    if any(token in haystack for token in ("BLUETOOTH", "STANDARD SERIAL OVER BLUETOOTH")):
        return 0
    vid = getattr(info, "vid", None)
    if vid == CH340_VID or port_matches_ch340(info):
        return 100
    if vid == ESPRESSIF_USB_VID:
        return 95
    if any(token in haystack for token in ("ESPRESSIF", "ESP32", "USB JTAG", "USB-TO-UART", "USB SERIAL", "USB CDC", "CP210")):
        return 90
    return 0


def discover_serial_port() -> str | None:
    """Return the most likely ESP32 serial port, covering CH340 and native USB CDC."""
    try:
        from serial.tools import list_ports  # type: ignore
        candidates = sorted(
            ((port_score(info), info.device) for info in list_ports.comports()),
            reverse=True,
        )
        for score, device in candidates:
            if score > 0:
                return device
    except Exception:
        pass
    return None


def discover_ch340_port() -> str | None:
    """Backward-compatible alias for serial auto-detection."""
    return discover_serial_port()


def describe_serial_port(port: str | None) -> str:
    if not port:
        return "none"
    return port


def wait_for_serial_ready(ser: object, timeout: float = 4.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode("utf-8", errors="replace").strip()
        if line:
            log(f"serial boot: {line}")
        if "Clawd Mochi Tank ready" in line or "Serial command examples" in line:
            return


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

    serial_port = discover_serial_port()
    print(f"serial auto-detect: {describe_serial_port(serial_port)}")

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


def send_anim_serial(anim: str, port: str | None = None, baud: int | None = None) -> bool:
    serial_port = (
        port
        or os.environ.get("CLAWD_TANK_SERIAL_PORT")
        or discover_serial_port()
    )
    if not serial_port:
        log("serial transport selected but no COM port was found; set CLAWD_TANK_SERIAL_PORT/--port")
        return False

    try:
        import serial  # type: ignore
    except ImportError:
        log("serial transport requires pyserial: python -m pip install pyserial")
        return False

    last_exc: Exception | None = None
    with SERIAL_LOCK:
        for attempt in range(3):
            try:
                ser = serial.Serial()
                ser.port = serial_port
                ser.baudrate = baud or int(os.environ.get("CLAWD_TANK_SERIAL_BAUD", DEFAULT_BAUD))
                ser.timeout = 0.4
                ser.rtscts = False
                ser.dsrdtr = False
                ser.dtr = False
                ser.rts = False
                got_ack = False
                with ser:
                    ser.dtr = False
                    ser.rts = False
                    ser.reset_input_buffer()
                    wait_for_serial_ready(ser)
                    ser.write(command_payload(anim).encode("utf-8"))
                    ser.flush()
                    deadline = time.time() + 1.5
                    while time.time() < deadline:
                        line = ser.readline().decode("utf-8", errors="replace").strip()
                        if "{" in line and "\"anim\"" in line:
                            log(f"serial state port={serial_port}: {line}")
                            got_ack = True
                            break
                if got_ack:
                    log(f"sent serial port={serial_port} anim={anim}")
                    return True
                last_exc = TimeoutError("serial command written but no firmware state ack received")
            except Exception as exc:
                last_exc = exc
                time.sleep(0.15 * (attempt + 1))
    log(f"serial send failed port={serial_port} anim={anim}: {last_exc}")
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
        if item == "serial":
            sent = send_anim_serial(anim, port=port, baud=baud)
        elif item == "ble":
            sent = send_anim_ble(anim, address=ble_address, name=ble_name)
        else:
            log(f"unknown transport={item!r}; expected serial, ble, auto, parallel, or comma list")
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
    args = argparse.Namespace(
        transport=transport,
        port=port,
        baud=baud,
        ble_address=ble_address,
        ble_name=ble_name,
        hub_url=None,
        no_hub=False,
        hub_required=False,
        client_id=DEFAULT_CLIENT_ID,
    )

    time.sleep(COMPLETE_DURATION_S)
    if read_last_event() > event_time + 1.0:
        log(f"timed transition aborted (new activity)")
        return
    deliver_anim(SESSION_IDLE_ANIM, args, event_time=event_time)
    log(f"timed transition: {TASK_COMPLETE_ANIM} -> {SESSION_IDLE_ANIM}")

    time.sleep(IDLE_DURATION_S)
    if read_last_event() > event_time + 1.0:
        log(f"timed transition aborted (new activity during idle)")
        return
    deliver_anim(SLEEP_ANIM, args, event_time=event_time)
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
    parser.add_argument("--transport", help="serial, ble, auto, parallel/all, or comma list")
    parser.add_argument("--port", help="optional serial port override; ESP32 serial is auto-detected when omitted")
    parser.add_argument("--baud", type=int, default=None)
    parser.add_argument("--ble-address")
    parser.add_argument("--ble-name", default=None)
    parser.add_argument("--hub-url", default=None)
    parser.add_argument("--no-hub", action="store_true", help="bypass local Clawd Hook Hub")
    parser.add_argument("--hub-required", action="store_true", help="do not fall back to direct transports if hub fails")
    parser.add_argument("--client-id", default=os.environ.get("CLAWD_TANK_CLIENT_ID", DEFAULT_CLIENT_ID))
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
        deliver_anim(args.test, args)
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
        deliver_anim(anim, args, payload=payload, event_time=event_time)
        # After Stop -> completion animation, spawn timer for idle then sleeping.
        if event == "Stop":
            spawn_timed_transition(event_time, args)
    else:
        ntype = payload.get("notification_type") or payload.get("type") or ""
        log(f"ignored payload event={payload.get('hook_event_name')!r} notification_type={ntype!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
