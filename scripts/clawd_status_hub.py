#!/usr/bin/env python3
"""Local Clawd hook hub with a small visual dashboard.

The hub accepts Codex/Claude hook deliveries on /hook, keeps transport state,
and forwards animations to the ESP32 by BLE or serial.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import importlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


LOG_DIR = Path.home() / ".clawd-mochi"
LOG_PATH = LOG_DIR / "status-hub.log"
PID_PATH = LOG_DIR / "status-hub.pid"
WATCH_PID_PATH = LOG_DIR / "session-watch.pid"
RUN_LOCK_PATH = LOG_DIR / "status-hub.run.lock"
EVENTS_LIMIT = 300
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def import_bridge():
    for name in ("codex_clawd_hook", "claude_clawd_hook"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise RuntimeError("could not import codex_clawd_hook or claude_clawd_hook")


bridge = import_bridge()


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{now_iso()} {message}\n")
    except OSError:
        pass


def write_pid() -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def acquire_file_lock(path: Path, stale_seconds: float = 30.0) -> bool:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()} {time.time():.6f}\n")
        return True
    except FileExistsError:
        try:
            parts = path.read_text(encoding="utf-8", errors="replace").split()
            pid = int(parts[0]) if parts else 0
            if pid and not pid_is_running(pid):
                path.unlink(missing_ok=True)
                return acquire_file_lock(path, stale_seconds)
            if time.time() - path.stat().st_mtime > stale_seconds:
                path.unlink(missing_ok=True)
                return acquire_file_lock(path, stale_seconds)
        except OSError:
            pass
        return False
    except OSError:
        return False


def release_file_lock(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def file_contains(path: Path, needle: str) -> bool:
    try:
        return needle in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def fmt_age(ts: float | None) -> str:
    if not ts:
        return ""
    age = max(0, int(time.time() - ts))
    if age < 60:
        return f"{age}s ago"
    if age < 3600:
        return f"{age // 60}m ago"
    return f"{age // 3600}h ago"


def process_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    return kwargs


def stop_pid(pid: int) -> None:
    if not pid_is_running(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


class BleSession:
    def __init__(self, name: str, address: str | None) -> None:
        self.name = name
        self.address = address
        self.client: Any = None
        self.target: str | None = address
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def send(self, anim: str, timeout: float = 8.0) -> tuple[bool, str]:
        fut = asyncio.run_coroutine_threadsafe(self._send(anim), self.loop)
        try:
            return fut.result(timeout=timeout)
        except Exception as exc:
            return False, str(exc)

    def scan(self, timeout: float = 6.0) -> tuple[bool, list[dict[str, Any]] | str]:
        fut = asyncio.run_coroutine_threadsafe(self._scan(), self.loop)
        try:
            return True, fut.result(timeout=timeout)
        except Exception as exc:
            return False, str(exc)

    def select(self, address: str, name: str | None = None) -> None:
        self.target = address.strip() or None
        self.address = self.target
        if name:
            self.name = name
        self.client = None

    def connect(self, timeout: float = 8.0) -> tuple[bool, str]:
        fut = asyncio.run_coroutine_threadsafe(self._connect(), self.loop)
        try:
            return fut.result(timeout=timeout)
        except Exception as exc:
            return False, str(exc)

    def disconnect(self, timeout: float = 4.0) -> tuple[bool, str]:
        fut = asyncio.run_coroutine_threadsafe(self._disconnect(), self.loop)
        try:
            return fut.result(timeout=timeout)
        except Exception as exc:
            return False, str(exc)

    def reset(self, clear_target: bool = False) -> None:
        self.disconnect()
        self.client = None
        if clear_target:
            self.target = None
            self.address = None

    def status(self) -> dict[str, Any]:
        connected = bool(self.client is not None and getattr(self.client, "is_connected", False))
        return {
            "name": self.name,
            "address": self.target,
            "connected": connected,
        }

    async def _scan(self) -> list[dict[str, Any]]:
        try:
            from bleak import BleakScanner  # type: ignore
        except ImportError:
            raise RuntimeError("bleak missing; install with: python -m pip install bleak")
        devices = await BleakScanner.discover(timeout=4.0, return_adv=True)
        rows: list[dict[str, Any]] = []
        for key, value in devices.items():
            device, adv = value
            name = device.name or getattr(adv, "local_name", "") or ""
            rows.append(
                {
                    "address": device.address,
                    "name": name,
                    "rssi": getattr(adv, "rssi", None),
                    "selected": device.address == self.target,
                    "suggested": bool(name and name.startswith(self.name)),
                }
            )
        rows.sort(key=lambda item: (not item["suggested"], item["name"] or "", item["address"]))
        return rows

    async def _send(self, anim: str) -> tuple[bool, str]:
        try:
            from bleak import BleakClient, BleakScanner  # type: ignore
        except ImportError:
            return False, "bleak missing; install with: python -m pip install bleak"

        try:
            ok, message = await self._connect()
            if not ok:
                return False, message
            await self.client.write_gatt_char(
                bridge.BLE_RX_UUID,
                bridge.command_payload(anim).encode("utf-8"),
                response=False,
            )
            return True, f"BLE {self.target}"
        except Exception as exc:
            self.client = None
            return False, f"BLE failed: {exc}"

    async def _connect(self) -> tuple[bool, str]:
        try:
            from bleak import BleakClient, BleakScanner  # type: ignore
        except ImportError:
            return False, "bleak missing; install with: python -m pip install bleak"

        if self.client is not None and self.client.is_connected:
            return True, f"BLE connected {self.target}"

        if not self.target:
            devices = await BleakScanner.discover(timeout=2.5)
            for device in devices:
                if (device.name or "").startswith(self.name):
                    self.target = device.address
                    self.address = self.target
                    break
        if not self.target:
            return False, f"BLE device not found name={self.name!r}"

        try:
            self.client = BleakClient(self.target, timeout=5.0)
            await self.client.connect()
            return True, f"BLE connected {self.target}"
        except Exception as exc:
            self.client = None
            return False, f"BLE connect failed: {exc}"

    async def _disconnect(self) -> tuple[bool, str]:
        if self.client is None:
            return True, "BLE disconnected"
        try:
            if self.client.is_connected:
                await self.client.disconnect()
            self.client = None
            return True, "BLE disconnected"
        except Exception as exc:
            self.client = None
            return False, f"BLE disconnect failed: {exc}"


class SerialSession:
    def __init__(self, port: str | None, baud: int | None) -> None:
        self.port = port
        self.baud = baud
        self.ser: Any = None
        self.lock = threading.RLock()

    def status(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "connected": bool(self.ser is not None and getattr(self.ser, "is_open", False)),
        }

    def select(self, port: str | None, baud: int | None = None) -> None:
        port = (port or "").strip() or None
        if port != self.port:
            self.disconnect()
        self.port = port
        if baud is not None:
            self.baud = baud

    def connect(self, port: str | None = None, baud: int | None = None) -> tuple[bool, str]:
        with self.lock:
            if port or baud is not None:
                self.select(port or self.port, baud)
            serial_port = self.port or bridge.discover_serial_port()
            if not serial_port:
                return False, "no serial port found"
            self.port = serial_port

            try:
                import serial  # type: ignore
            except ImportError:
                return False, "pyserial missing; install with: python -m pip install pyserial"

            try:
                if self.ser is not None and getattr(self.ser, "is_open", False):
                    return True, f"serial connected {self.port}"
                ser = serial.Serial()
                ser.port = self.port
                ser.baudrate = self.baud or int(os.environ.get("CLAWD_TANK_SERIAL_BAUD", bridge.DEFAULT_BAUD))
                ser.timeout = 0.4
                ser.write_timeout = 1.0
                ser.rtscts = False
                ser.dsrdtr = False
                ser.dtr = False
                ser.rts = False
                ser.open()
                ser.dtr = False
                ser.rts = False
                ser.reset_input_buffer()
                bridge.wait_for_serial_ready(ser)
                self.ser = ser
                return True, f"serial connected {self.port}"
            except Exception as exc:
                self._close_locked()
                return False, f"serial connect failed {self.port}: {exc}"

    def disconnect(self) -> tuple[bool, str]:
        with self.lock:
            self._close_locked()
            return True, "serial disconnected"

    def send(self, anim: str) -> tuple[bool, str]:
        with self.lock:
            if self.ser is None or not getattr(self.ser, "is_open", False):
                ok, message = self.connect(self.port, self.baud)
                if not ok:
                    return False, message
            try:
                self.ser.write(bridge.command_payload(anim).encode("utf-8"))
                self.ser.flush()
                deadline = time.time() + 1.5
                while time.time() < deadline:
                    line = self.ser.readline().decode("utf-8", errors="replace").strip()
                    if "{" in line and "\"anim\"" in line:
                        return True, f"serial delivered {self.port}"
                return False, "serial command written but no firmware state ack received"
            except Exception as exc:
                self._close_locked()
                return False, f"serial failed {self.port}: {exc}"

    def _close_locked(self) -> None:
        if self.ser is not None:
            try:
                if getattr(self.ser, "is_open", False):
                    self.ser.close()
            except Exception:
                pass
        self.ser = None


class HubState:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.hooks: dict[str, dict[str, Any]] = {}
        self.clients: dict[str, dict[str, Any]] = {}
        self.transports: dict[str, dict[str, Any]] = {}
        self.state: dict[str, Any] = {
            "started_at": now_iso(),
            "current_anim": None,
            "current_source": None,
            "current_client_id": None,
            "current_client_kind": None,
            "current_event": None,
            "current_tool": None,
            "transport": None,
            "transport_status": "idle",
            "transport_message": "",
            "last_error": None,
            "last_hook_at": None,
            "last_send_ms": None,
            "delivered_count": 0,
            "failed_count": 0,
        }
        self.ble = BleSession(args.ble_name, args.ble_address)
        self.serial = SerialSession(args.port, args.baud)

    def scan_serial(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            from serial.tools import list_ports  # type: ignore
            for info in list_ports.comports():
                device = str(getattr(info, "device", "") or "")
                rows.append(
                    {
                        "device": device,
                        "description": str(getattr(info, "description", "") or ""),
                        "hwid": str(getattr(info, "hwid", "") or ""),
                        "manufacturer": str(getattr(info, "manufacturer", "") or ""),
                        "product": str(getattr(info, "product", "") or ""),
                        "selected": device == (self.args.port or ""),
                        "suggested": bool(bridge.port_score(info) >= 90),
                    }
                )
        except Exception as exc:
            return [{"error": str(exc)}]
        rows.sort(key=lambda item: (not item.get("suggested"), item.get("device") or ""))
        return rows

    def scan_ble(self) -> dict[str, Any]:
        ok, result = self.ble.scan()
        if ok:
            return {"ok": True, "devices": result}
        return {"ok": False, "error": result, "devices": []}

    def config(self) -> dict[str, Any]:
        ble_status = self.ble.status()
        serial_status = self.serial.status()
        return {
            "transport": self.args.transport,
            "serial_port": serial_status["port"],
            "baud": self.args.baud,
            "serial_connected": serial_status["connected"],
            "ble_name": ble_status["name"],
            "ble_address": ble_status["address"],
            "ble_connected": ble_status["connected"],
        }

    def update_config(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if "serial_port" in data:
                port = str(data.get("serial_port") or "").strip()
                self.serial.select(port or None, self.args.baud)
                self.args.port = self.serial.port
                self.transports["serial"] = {
                    "status": "selected" if port else "idle",
                    "message": f"{port} selected; connect to hold it" if port else "auto serial detection",
                    "last_at": time.time(),
                }
            if "ble_address" in data:
                address = str(data.get("ble_address") or "").strip()
                name = str(data.get("ble_name") or "").strip()
                self.ble.select(address, name or None)
                self.transports["ble"] = {
                    "status": "selected" if address else "idle",
                    "message": address or "auto BLE discovery",
                    "last_at": time.time(),
                }
            if "transport" in data:
                transport = str(data.get("transport") or "").strip().lower()
                if transport:
                    self.args.transport = transport
            return self.config()

    def connect_transport(self, data: dict[str, Any]) -> dict[str, Any]:
        transport = str(data.get("transport") or "").strip().lower()
        if transport == "serial":
            port = str(data.get("serial_port") or data.get("port") or "").strip()
            ok, message = self.serial.connect(port or None, self.args.baud)
            with self.lock:
                self.args.port = self.serial.port
                self.transports["serial"] = {
                    "status": "connected" if ok else "failed",
                    "message": message,
                    "last_at": time.time(),
                }
            return {"ok": ok, "transport": "serial", "message": message, "config": self.config()}

        if transport == "ble":
            address = str(data.get("ble_address") or data.get("address") or "").strip()
            name = str(data.get("ble_name") or data.get("name") or "").strip()
            if address or name:
                self.ble.select(address, name or None)
            ok, message = self.ble.connect()
            with self.lock:
                self.transports["ble"] = {
                    "status": "connected" if ok else "failed",
                    "message": message,
                    "last_at": time.time(),
                }
            return {"ok": ok, "transport": "ble", "message": message, "config": self.config()}

        return {"ok": False, "error": "expected transport serial or ble"}

    def disconnect_transport(self, data: dict[str, Any]) -> dict[str, Any]:
        transport = str(data.get("transport") or "").strip().lower()
        if transport == "serial":
            clear = bool(data.get("clear"))
            ok, message = self.serial.disconnect()
            with self.lock:
                if clear:
                    self.serial.select(None, self.args.baud)
                self.args.port = self.serial.port
                self.transports["serial"] = {
                    "status": "idle" if ok else "failed",
                    "message": message,
                    "last_at": time.time(),
                }
            return {"ok": ok, "transport": "serial", "message": message, "config": self.config()}

        if transport == "ble":
            clear = bool(data.get("clear"))
            ok, message = self.ble.disconnect()
            if clear:
                self.ble.reset(clear_target=True)
            with self.lock:
                self.transports["ble"] = {
                    "status": "idle" if ok else "failed",
                    "message": message,
                    "last_at": time.time(),
                }
            return {"ok": ok, "transport": "ble", "message": message, "config": self.config()}

        return {"ok": False, "error": "expected transport serial or ble"}

    def restart_watcher(self) -> dict[str, Any]:
        watcher = Path(__file__).with_name("codex_session_watch.py")
        if not watcher.exists():
            watcher = Path.home() / ".codex" / "skills" / "codex-clawd-status" / "scripts" / "codex_session_watch.py"
        if not watcher.exists():
            return {"ok": False, "error": "codex_session_watch.py not found"}

        old_pid = read_pid(WATCH_PID_PATH)
        stop_pid(old_pid)
        time.sleep(0.3)
        proc = subprocess.Popen([sys.executable, str(watcher), "--follow-latest"], **process_kwargs())
        log(f"module restart codex-watcher old_pid={old_pid} new_pid={proc.pid}")
        return {"ok": True, "module": "codex-watcher", "pid": proc.pid}

    def restart_ble(self) -> dict[str, Any]:
        with self.lock:
            self.ble.reset()
            self.transports["ble"] = {
                "status": "idle",
                "message": "BLE connection reset",
                "last_at": time.time(),
            }
        log("module restart transport-ble")
        return {"ok": True, "module": "transport-ble"}

    def restart_module(self, module: str, server: ThreadingHTTPServer | None = None) -> dict[str, Any]:
        if module == "codex-watcher":
            return self.restart_watcher()
        if module == "transport-ble":
            return self.restart_ble()
        if module == "hub":
            if server is None:
                return {"ok": False, "error": "server handle unavailable"}
            self.schedule_hub_restart(server)
            return {"ok": True, "module": "hub", "message": "Hub restarting"}
        return {"ok": False, "error": f"module {module!r} is not restartable"}

    def schedule_hub_restart(self, server: ThreadingHTTPServer) -> None:
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--host",
            str(self.args.host),
            "--port",
            str(self.args.hub_port),
            "--transport",
            str(self.args.transport),
        ]
        if self.args.port:
            cmd += ["--serial-port", str(self.args.port)]
        if self.args.baud is not None:
            cmd += ["--baud", str(self.args.baud)]
        if self.ble.target:
            cmd += ["--ble-address", str(self.ble.target)]
        if self.ble.name:
            cmd += ["--ble-name", str(self.ble.name)]

        helper = (
            "import subprocess,time;"
            "time.sleep(1.2);"
            f"subprocess.Popen({cmd!r}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True"
            + (", creationflags=0x00000008|0x08000000" if os.name == "nt" else ", start_new_session=True")
            + ")"
        )
        subprocess.Popen([sys.executable, "-c", helper], **process_kwargs())

        def stop_server() -> None:
            time.sleep(0.2)
            log("module restart hub")
            server.shutdown()

        threading.Thread(target=stop_server, daemon=True).start()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                **self.state,
                "hooks": self.hooks,
                "clients": self.clients,
                "transports": self.transports,
                "modules": self.modules_locked(),
                "events_count": len(self.events),
                "hub_pid": os.getpid(),
            }

    def modules_locked(self) -> dict[str, dict[str, Any]]:
        codex_hooks = Path.home() / ".codex" / "hooks.json"
        claude_settings = Path.home() / ".claude" / "settings.json"
        watcher_pid = read_pid(WATCH_PID_PATH)
        serial_port = ""
        try:
            serial_port = bridge.discover_serial_port() or ""
        except Exception:
            serial_port = ""

        def client_module(client_id: str, label: str, configured: bool) -> dict[str, Any]:
            client = self.clients.get(client_id, {})
            last_at = client.get("last_at")
            if client:
                status = client.get("status") or "seen"
                detail = f"last {client.get('last_anim') or ''} {fmt_age(last_at)}".strip()
            else:
                status = "configured" if configured else "missing"
                detail = "waiting for first event" if configured else "hook config not found"
            return {"label": label, "status": status, "detail": detail, "last_at": last_at}

        transport_modules = {}
        serial_status = self.serial.status()
        for name in bridge.transport_list(self.args.transport):
            t = self.transports.get(name, {})
            selected_serial = serial_status["port"] or self.args.port or serial_port
            if name == "serial" and serial_status["connected"]:
                status = "connected"
            else:
                status = t.get("status") or ("available" if name == "serial" and selected_serial else "idle")
            detail = t.get("message") or ""
            if name == "serial" and selected_serial:
                detail = detail if str(detail).startswith(str(selected_serial)) else f"{selected_serial} {detail}".strip()
            if name == "ble" and self.ble.target:
                detail = f"{self.ble.target} {detail}".strip()
            transport_modules[f"transport-{name}"] = {
                "label": f"Transport {name.upper()}",
                "status": status,
                "detail": detail,
                "last_at": t.get("last_at"),
                "restartable": name == "ble",
            }

        device_status = self.state.get("transport_status") or "idle"
        device_detail = self.state.get("transport_message") or "waiting for delivery"
        modules = {
            "hub": {
                "label": "Hook Hub",
                "status": "online",
                "detail": f"pid {os.getpid()} / transport {self.args.transport}",
                "last_at": time.time(),
                "restartable": True,
            },
            "codex-hook": client_module(
                "codex-code",
                "Codex native hook",
                file_contains(codex_hooks, "codex_clawd_hook.py"),
            ),
            "codex-vscode": client_module("codex-vscode", "Codex VS Code watcher", True),
            "codex-desktop": client_module("codex-desktop", "Codex Desktop watcher", True),
            "codex-watcher": {
                "label": "Codex session watcher",
                "status": "online" if pid_is_running(watcher_pid) else "offline",
                "detail": f"pid {watcher_pid}" if watcher_pid else "pid file missing",
                "last_at": None,
                "restartable": True,
            },
            "claude-hook": client_module(
                "claude-code",
                "Claude Code hook",
                file_contains(claude_settings, "claude_clawd_hook.py"),
            ),
            "esp32": {
                "label": "ESP32 display",
                "status": device_status,
                "detail": device_detail,
                "last_at": self.state.get("last_hook_at"),
            },
        }
        modules.update(transport_modules)
        return modules

    def recent_events(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.events)

    def add_event(self, item: dict[str, Any]) -> None:
        with self.lock:
            self.events.append(item)
            if len(self.events) > EVENTS_LIMIT:
                del self.events[: len(self.events) - EVENTS_LIMIT]

    def deliver(self, delivery: dict[str, Any]) -> dict[str, Any]:
        anim = str(delivery.get("anim") or "").strip()
        if not anim:
            return {"ok": False, "error": "missing anim"}

        payload = delivery.get("payload") if isinstance(delivery.get("payload"), dict) else {}
        source = str(delivery.get("source") or "manual")
        client_id = str(delivery.get("client_id") or source or "manual")
        client_kind = str(delivery.get("client_kind") or source or "manual")
        event = str(delivery.get("event") or payload.get("hook_event_name") or payload.get("event") or "")
        tool = str(delivery.get("tool") or payload.get("tool_name") or payload.get("toolName") or "")
        ts = time.time()
        hook_key = f"{client_id}:{event}" if event else client_id

        with self.lock:
            client = self.clients.setdefault(
                client_id,
                {
                    "client_id": client_id,
                    "kind": client_kind,
                    "source": source,
                    "status": "idle",
                    "hooks": {},
                    "delivered_count": 0,
                    "failed_count": 0,
                    "last_at": None,
                },
            )
            client.update({"kind": client_kind, "source": source, "status": "sending", "last_at": ts})
            self.state.update(
                {
                    "current_anim": anim,
                    "current_source": source,
                    "current_client_id": client_id,
                    "current_client_kind": client_kind,
                    "current_event": event,
                    "current_tool": tool,
                    "transport_status": "sending",
                    "last_hook_at": ts,
                    "last_error": None,
                }
            )
            if event:
                hook_state = {
                    "status": "sending",
                    "last_anim": anim,
                    "last_tool": tool,
                    "last_source": source,
                    "last_client_id": client_id,
                    "last_client_kind": client_kind,
                    "last_at": ts,
                }
                self.hooks[hook_key] = {"event": event, **hook_state}
                client["hooks"][event] = hook_state

        started = time.perf_counter()
        results = []
        sent = False
        for transport in bridge.transport_list(self.args.transport):
            ok, message = self.send_by_transport(anim, transport)
            results.append({"transport": transport, "ok": ok, "message": message})
            if ok:
                sent = True
                if self.args.transport != "parallel":
                    break

        elapsed_ms = round((time.perf_counter() - started) * 1000)
        status = "delivered" if sent else "failed"
        event_item = {
            "at": now_iso(),
            "source": source,
            "client_id": client_id,
            "client_kind": client_kind,
            "event": event,
            "tool": tool,
            "anim": anim,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "results": results,
        }
        self.add_event(event_item)

        transport_message = "; ".join(r["message"] for r in results)
        with self.lock:
            for result in results:
                self.transports[result["transport"]] = {
                    "status": "delivered" if result["ok"] else "failed",
                    "message": result["message"],
                    "last_at": ts,
                }
            self.state.update(
                {
                    "transport": next((r["transport"] for r in results if r["ok"]), None),
                    "transport_status": status,
                    "transport_message": transport_message,
                    "last_send_ms": elapsed_ms,
                    "last_error": None if sent else transport_message,
                }
            )
            self.state["delivered_count" if sent else "failed_count"] += 1
            client = self.clients.setdefault(client_id, {"hooks": {}})
            client["status"] = status
            client["last_anim"] = anim
            client["last_event"] = event
            client["last_tool"] = tool
            client["last_at"] = ts
            client["delivered_count"] = int(client.get("delivered_count", 0)) + (1 if sent else 0)
            client["failed_count"] = int(client.get("failed_count", 0)) + (0 if sent else 1)
            if event:
                self.hooks[hook_key]["status"] = status
                client.setdefault("hooks", {}).setdefault(event, {})["status"] = status

        log(f"{status} client={client_id} source={source} event={event!r} tool={tool!r} anim={anim} results={results}")
        return {"ok": sent, "status": status, "elapsed_ms": elapsed_ms, "results": results}

    def send_by_transport(self, anim: str, transport: str) -> tuple[bool, str]:
        if transport == "ble":
            return self.ble.send(anim)
        if transport == "serial":
            ok, message = self.serial.send(anim)
            self.args.port = self.serial.port
            return ok, message
        return False, f"unknown transport {transport!r}"


INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clawd Hook Hub</title>
<style>
:root{--bg:#0d0f13;--panel:#15181f;--raised:#1b1f28;--line:#272d38;--text:#eef1f5;--muted:#8c95a3;--accent:#d97757;--accent2:#e8927c;--ok:#5fd39a;--bad:#ff7c6c;--warn:#f3c552}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 72% -12%,#1a1d26 0%,var(--bg) 55%);color:var(--text);font-family:"Segoe UI",system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:1400px;margin:0 auto;padding:0 24px 44px}
.top{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:13px;padding:16px 0;margin-bottom:6px;background:linear-gradient(180deg,var(--bg) 72%,transparent)}
.logo{width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,var(--accent),#b85a3e);display:flex;align-items:center;justify-content:center;font-weight:800;color:#fff;box-shadow:0 4px 14px rgba(217,119,87,.4)}
.title{display:flex;flex-direction:column;line-height:1.15}
.title b{font-size:19px;letter-spacing:.2px}
.title span{font-size:12px;color:var(--muted)}
.spacer{flex:1}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(400px,1fr));gap:20px}
.panel{border:1px solid var(--line);border-radius:16px;padding:20px;background:linear-gradient(180deg,var(--panel),#12151b);box-shadow:0 1px 0 rgba(255,255,255,.02) inset,0 10px 26px rgba(0,0,0,.28);transition:transform 0.2s,box-shadow 0.2s}
.panel:hover{transform:translateY(-2px);box-shadow:0 12px 32px rgba(0,0,0,.4)}
.panel.wide{grid-column:1/-1}
.ph{display:flex;align-items:center;gap:9px;margin:0 0 13px;font-size:11.5px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;color:var(--muted)}
.ph::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--accent);box-shadow:0 0 9px var(--accent)}
.hero{display:flex;align-items:center;gap:12px;margin-bottom:14px}
.hero-anim{font-size:36px;font-weight:800;letter-spacing:.3px;background:linear-gradient(90deg,#fff,var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent}
.metas{display:grid;grid-template-columns:1fr 1fr;gap:9px 16px}
.meta{display:flex;flex-direction:column;gap:2px;min-width:0}
.ml{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.mv{font-size:14px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;padding:3px 11px;border-radius:999px;background:#22262f;color:var(--muted);border:1px solid var(--line)}
.pill::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor;flex:none}
.pill.ok{color:var(--ok);background:rgba(95,211,154,.10);border-color:rgba(95,211,154,.28)}
.pill.bad{color:var(--bad);background:rgba(255,124,108,.10);border-color:rgba(255,124,108,.28)}
.pill.send{color:var(--warn);background:rgba(243,197,82,.10);border-color:rgba(243,197,82,.28)}
.pill.muted{color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.chip{font-size:12px;font-weight:600;background:var(--raised);color:#cfd5de;border:1px solid var(--line);border-radius:8px;padding:7px 11px;cursor:pointer;transition:.15s}
.chip:hover{border-color:var(--accent);color:#fff;transform:translateY(-1px)}
.chip.active{background:linear-gradient(135deg,var(--accent),#b85a3e);border-color:transparent;color:#fff;box-shadow:0 4px 12px rgba(217,119,87,.32)}
.btn{font-size:12px;font-weight:600;background:var(--raised);color:#cfd5de;border:1px solid var(--line);border-radius:8px;padding:6px 11px;margin:2px 2px 2px 0;cursor:pointer;transition:.15s}
.btn:hover{border-color:var(--accent);color:#fff}
.sub{font-size:10.5px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin:15px 0 7px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600;text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:8px;border-bottom:1px solid rgba(39,45,56,.55);color:#dfe4ea}
tr:last-child td{border-bottom:none}
tbody tr:hover td,table tr:hover td{background:rgba(217,119,87,.05)}
.mono{font-family:"Cascadia Code",Consolas,ui-monospace,monospace;font-size:12px;color:var(--muted)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.send{color:var(--warn)}.muted,.k{color:var(--muted)}
.empty{color:var(--muted);font-size:13px;padding:10px 2px;text-align:center}
p{margin:7px 0}
::-webkit-scrollbar{width:9px;height:9px}::-webkit-scrollbar-thumb{background:#2a313c;border-radius:6px}
</style></head><body><main>
<header class="top">
<div class="logo">C</div>
<div class="title"><b>Clawd Hook Hub</b><span>local animation router</span></div>
<div class="spacer"></div>
<span id="status" class="pill muted">loading</span>
</header>
<div class="grid">
<section class="panel"><div class="ph">Current</div><div id="current"></div><div class="sub">Manual trigger</div><div id="buttons" class="chips"></div></section>
<section class="panel"><div class="ph">Transports</div><div id="transport"></div><div class="sub">Select device</div><div id="selectors"></div></section>
<section class="panel"><div class="ph">Modules</div><table id="modules"></table></section>
<section class="panel"><div class="ph">Clients</div><table id="clients"></table></section>
<section class="panel"><div class="ph">Hooks</div><table id="hooks"></table></section>
<section class="panel wide"><div class="ph">Events</div><table id="events"></table></section>
</div>
<script>
const anims=["idle","thinking","typing","building","debugger","wizard","conducting","juggling","confused","sweeping","happy","sleeping","beacon","alert","dizzy"];
buttons.innerHTML=anims.map(a=>`<button class="chip" data-a="${a}" onclick="send('${a}')">${a}</button>`).join("");
async function send(anim){await fetch('/send',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({anim})}); refresh();}
async function post(url,body){return await (await fetch(url,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body||{})})).json();}
function cls(s){return ["delivered","online","available","configured","selected","connected"].includes(s)?"ok":["failed","offline","missing"].includes(s)?"bad":s==="sending"?"send":"muted"}
function pill(s){return `<span class="pill ${cls(s)}">${s||"idle"}</span>`}
function meta(l,v){const t=(v===0?"0":v||"").toString().replace(/"/g,"");return `<div class="meta"><span class="ml">${l}</span><span class="mv" title="${t}">${(v===0?"0":v)||""}</span></div>`}
function none(t){return `<tr><td class="empty" colspan="9">${t}</td></tr>`}
function q(s){return JSON.stringify(s||"")}
function selected(a,b){return (a||"")===(b||"")}
function modeLabel(mode){return mode==="ble"?"BLE only":mode==="serial"?"Serial only":"Auto: BLE -> Serial"}
let selectorView="home";
async function setTransport(mode){await post('/config',{transport:mode}); refresh();}
function backSelectors(){selectorView="home"; refresh();}
async function connectSerial(port){
  const result=await post('/connect',{transport:'serial',serial_port:port});
  if(!result.ok){alert(result.error||result.message||'serial connect failed');}
  refresh();
}
async function disconnectSerial(){await post('/disconnect',{transport:'serial'}); refresh();}
async function clearSerial(){await post('/disconnect',{transport:'serial',clear:true}); refresh();}
async function connectBle(address,name){
  const result=await post('/connect',{transport:'ble',ble_address:address,ble_name:name});
  if(!result.ok){alert(result.error||result.message||'BLE connect failed');}
  refresh();
}
async function disconnectBle(clear){await post('/disconnect',{transport:'ble',clear:!!clear}); refresh();}
function renderSelectors(cfg){
  const mode=cfg.transport||"auto";
  selectors.innerHTML=
    `<div class="sub">Mode</div>`+
    `<button class="btn ${mode==="auto"?"ok":""}" onclick="setTransport('auto')">Auto BLE -> Serial</button>`+
    `<button class="btn ${mode==="ble"?"ok":""}" onclick="setTransport('ble')">BLE only</button>`+
    `<button class="btn ${mode==="serial"?"ok":""}" onclick="setTransport('serial')">Serial only</button>`+
    `<div class="sub">Serial</div>`+
    `<p>${pill(cfg.serial_connected?"connected":(cfg.serial_port?"selected":"idle"))} <span class="mono">${cfg.serial_port||"auto detect"}</span></p>`+
    `<p class="muted">Connect holds the serial port open until Disconnect releases it.</p>`+
    `<button class="btn" onclick="scanSerial()">Search serial</button>`+
    `<button class="btn" onclick="connectSerial(null)">Connect auto serial</button>`+
    `<button class="btn" onclick="disconnectSerial()">Disconnect serial</button>`+
    `<button class="btn" onclick="clearSerial()">Clear serial</button>`+
    `<div class="sub">BLE</div>`+
    `<p>${pill(cfg.ble_connected?"connected":(cfg.ble_address||cfg.ble_name?"selected":"idle"))} <span class="mono">${cfg.ble_name||"ClawdTank"} ${cfg.ble_address||""}</span></p>`+
    `<button class="btn" onclick="scanBle()">Search BLE</button>`+
    `<button class="btn" onclick="connectBle(null,null)">Connect BLE</button>`+
    `<button class="btn" onclick="disconnectBle(false)">Disconnect BLE</button>`;
}
async function scanSerial(){
  selectorView="serial";
  selectors.innerHTML='<p class="muted">Scanning serial...</p>';
  const rows=await (await fetch('/scan/serial')).json();
  const cfg=await (await fetch('/config')).json();
  selectors.innerHTML='<div class="sub">Serial</div>'+
    rows.map(r=>r.error?`<p class=bad>${r.error}</p>`:
      `<p><button class="btn" onclick="connectSerial(${q(r.device)})">Connect / hold</button> ${selected(cfg.serial_port,r.device)?pill(cfg.serial_connected?"connected":"selected"):""} <b>${r.device}</b> ${r.suggested?'<span class=ok>ESP32</span> ':''}<span class="mono">${r.description||''} ${r.hwid||''}</span></p>`).join('')+
    '<button class="btn" onclick="connectSerial(null)">Connect auto serial</button> <button class="btn" onclick="disconnectSerial()">Disconnect serial</button> <button class="btn" onclick="clearSerial()">Clear serial</button> <button class="btn" onclick="backSelectors()">Back</button>';
}
async function scanBle(){
  selectorView="ble";
  selectors.innerHTML='<p class="muted">Scanning BLE...</p>';
  const data=await (await fetch('/scan/ble')).json();
  if(!data.ok){selectors.innerHTML=`<p class=bad>${data.error}</p>`;return}
  const cfg=await (await fetch('/config')).json();
  selectors.innerHTML='<div class="sub">BLE</div>'+
    data.devices.map(r=>`<p><button class="btn" onclick="connectBle(${q(r.address)},${q(r.name||'')})">Connect</button> ${selected(cfg.ble_address,r.address)?pill(cfg.ble_connected?"connected":"selected"):""} <b>${r.name||'(unnamed)'}</b> <span class="mono">${r.address} ${r.rssi??''}</span> ${r.suggested?'<span class=ok>target</span>':''}</p>`).join('')+
    '<button class="btn" onclick="connectBle(null,null)">Connect BLE auto</button> <button class="btn" onclick="disconnectBle(false)">Disconnect BLE</button> <button class="btn" onclick="backSelectors()">Back</button>';
}
async function selectSerial(port){await post('/config',{serial_port:port}); refresh();}
async function selectBle(address,name){await post('/config',{ble_address:address,ble_name:name}); refresh();}
async function restartModule(name){
  const result=await post('/module/restart',{module:name});
  if(!result.ok){alert(result.error||'restart failed');return}
  if(name==='hub'){status.textContent='restarting'; setTimeout(refresh,2500); return}
  refresh();
}
async function refresh(){
  const s=await (await fetch('/state')).json();
  const cfg=await (await fetch('/config')).json();
  status.textContent=s.transport_status||"idle"; status.className="pill "+cls(s.transport_status);
  current.innerHTML=`<div class="hero"><div class="hero-anim">${s.current_anim||"idle"}</div>${pill(s.transport_status)}</div>`+
    `<div class="metas">`+meta("client",(s.current_client_id||"")+(s.current_client_kind?" / "+s.current_client_kind:""))+meta("source",s.current_source)+meta("event",s.current_event)+meta("tool",s.current_tool)+`</div>`;
  document.querySelectorAll('#buttons .chip').forEach(b=>b.classList.toggle('active',b.dataset.a===s.current_anim));
  const me=Object.entries(s.modules||{});
  modules.innerHTML='<tr><th>Module</th><th>Status</th><th>Detail</th><th></th></tr>'+(me.length?me.map(([k,v])=>`<tr><td>${v.label||k}</td><td>${pill(v.status)}</td><td class="muted">${v.detail||""}</td><td>${v.restartable?`<button class="btn" onclick="restartModule('${k}')">Restart</button>`:""}</td></tr>`).join(""):none("no modules"));
  transport.innerHTML=`<div class="metas">`+
    meta("mode",modeLabel(cfg.transport))+
    meta("serial",cfg.serial_connected?`connected ${cfg.serial_port||""}`:(cfg.serial_port?`selected ${cfg.serial_port}`:"auto detect"))+
    meta("BLE",`${cfg.ble_connected?"connected ":""}${cfg.ble_name||"ClawdTank"} ${cfg.ble_address||""}`)+
    meta("last transport",s.transport)+meta("message",s.transport_message)+meta("latency",s.last_send_ms!=null?s.last_send_ms+" ms":null)+meta("delivered / failed",`<span class=ok>${s.delivered_count||0}</span> / <span class=bad>${s.failed_count||0}</span>`)+`</div>`;
  if(selectorView==="home"){renderSelectors(cfg)}
  const ce=Object.entries(s.clients||{});
  clients.innerHTML='<tr><th>Client</th><th>Kind</th><th>Status</th><th>Anim</th><th>Event</th><th>OK / Fail</th></tr>'+(ce.length?ce.map(([k,v])=>`<tr><td>${k}</td><td class="muted">${v.kind||""}</td><td>${pill(v.status)}</td><td>${v.last_anim||""}</td><td class="muted">${v.last_event||""}</td><td><span class=ok>${v.delivered_count||0}</span> / <span class=bad>${v.failed_count||0}</span></td></tr>`).join(""):none("no clients yet"));
  const hk=Object.values(Object.entries(s.hooks||{}).reduce((acc,[k,v])=>{const id=v.last_client_id||k;if(!acc[id]||v.last_at>acc[id].last_at)acc[id]=v;return acc},{}));
  hooks.innerHTML='<tr><th>Client</th><th>Hook</th><th>Status</th><th>Anim</th><th>Tool</th><th>Source</th></tr>'+(hk.length?hk.map(v=>`<tr><td>${v.last_client_id||""}</td><td>${v.event||""}</td><td>${pill(v.status)}</td><td>${v.last_anim||""}</td><td class="muted">${v.last_tool||""}</td><td class="muted">${v.last_source||""}</td></tr>`).join(""):none("no hooks yet"));
  const ev=await (await fetch('/events')).json();
  events.innerHTML='<tr><th>Time</th><th>Client</th><th>Source</th><th>Event</th><th>Anim</th><th>Status</th></tr>'+(ev.length?ev.slice(-40).reverse().map(e=>`<tr><td class="mono">${e.at}</td><td>${e.client_id||""}</td><td class="muted">${e.source}</td><td>${e.event}</td><td>${e.anim}</td><td>${pill(e.status)}</td></tr>`).join(""):none("no events yet"));
}
setInterval(refresh,1000); refresh();
</script></main></body></html>"""


class HubHandler(BaseHTTPRequestHandler):
    hub: HubState

    def log_message(self, fmt: str, *args: Any) -> None:
        log(fmt % args)

    def send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/state":
            self.send_json(self.hub.snapshot())
        elif path == "/events":
            self.send_json(self.hub.recent_events())
        elif path == "/modules":
            with self.hub.lock:
                self.send_json(self.hub.modules_locked())
        elif path == "/scan/serial":
            self.send_json(self.hub.scan_serial())
        elif path == "/scan/ble":
            self.send_json(self.hub.scan_ble())
        elif path == "/config":
            self.send_json(self.hub.config())
        elif path == "/health":
            self.send_json({"ok": True, "pid": os.getpid()})
        else:
            self.send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            data = self.read_json()
            if path == "/hook":
                self.send_json(self.hub.deliver(data))
            elif path == "/send":
                data.setdefault("source", "manual")
                data.setdefault("client_id", "manual")
                data.setdefault("client_kind", "manual")
                self.send_json(self.hub.deliver(data))
            elif path == "/config":
                self.send_json(self.hub.update_config(data))
            elif path == "/connect":
                self.send_json(self.hub.connect_transport(data))
            elif path == "/disconnect":
                self.send_json(self.hub.disconnect_transport(data))
            elif path == "/module/restart":
                module = str(data.get("module") or "")
                self.send_json(self.hub.restart_module(module, self.server))
            else:
                self.send_json({"ok": False, "error": "not found"}, 404)
        except Exception as exc:
            log(f"request failed: {exc}")
            self.send_json({"ok": False, "error": str(exc)}, 500)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("CLAWD_TANK_HUB_HOST", DEFAULT_HOST))
    parser.add_argument("--port", dest="hub_port", type=int, default=int(os.environ.get("CLAWD_TANK_HUB_PORT", DEFAULT_PORT)))
    parser.add_argument("--transport", default=os.environ.get("CLAWD_TANK_TRANSPORT", "auto"))
    parser.add_argument("--serial-port", dest="port_override", default=None)
    parser.add_argument("--baud", type=int, default=None)
    parser.add_argument("--ble-address", default=os.environ.get("CLAWD_TANK_BLE_ADDRESS"))
    parser.add_argument("--ble-name", default=os.environ.get("CLAWD_TANK_BLE_NAME", bridge.DEFAULT_BLE_NAME))
    args = parser.parse_args()
    args.port = args.port_override

    try:
        if not acquire_file_lock(RUN_LOCK_PATH):
            log("hub start skipped; run lock is active")
            return 0
        HubHandler.hub = HubState(args)
        try:
            server = ThreadingHTTPServer((args.host, args.hub_port), HubHandler)
        except OSError as exc:
            log(f"hub bind skipped http://{args.host}:{args.hub_port}: {exc}")
            return 0
        write_pid()
        log(f"hub listening http://{args.host}:{args.hub_port} transport={args.transport}")
        print(f"Clawd Hook Hub: http://{args.host}:{args.hub_port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return 0
    finally:
        release_file_lock(RUN_LOCK_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

