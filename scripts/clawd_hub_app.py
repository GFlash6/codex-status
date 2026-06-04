#!/usr/bin/env python3
"""Small background UI controller for the Clawd Hook Hub.

The app keeps the local Hub and Codex session watcher alive, shows module
status, and opens the web dashboard. It uses Tkinter by default and adds a
system tray icon when pystray is installed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import ttk
from typing import Any


APP_NAME = "Clawd Hub"
DEFAULT_HUB_URL = "http://127.0.0.1:8765"
LOG_DIR = Path.home() / ".clawd-mochi"
APP_PID_PATH = LOG_DIR / "hub-app.pid"
HUB_START_LOCK_PATH = LOG_DIR / "status-hub.start.lock"
SCRIPT_DIR = Path(__file__).resolve().parent
HUB_SCRIPT = SCRIPT_DIR / "clawd_status_hub.py"
WATCHER_SCRIPT = SCRIPT_DIR / "codex_session_watch.py"
NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def process_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    return kwargs


def write_pid() -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        APP_PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


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


def http_json(url: str, method: str = "GET", body: dict[str, Any] | None = None, timeout: float = 2.0) -> dict[str, Any]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class HubController:
    def __init__(self, hub_url: str, transport: str) -> None:
        self.hub_url = hub_url.rstrip("/")
        self.transport = transport

    def health(self) -> dict[str, Any]:
        return http_json(self.hub_url + "/health", timeout=1.0)

    def modules(self) -> dict[str, Any]:
        return http_json(self.hub_url + "/modules", timeout=2.0)

    def ensure_hub(self) -> None:
        try:
            self.health()
            return
        except Exception:
            pass
        if not acquire_start_lock(HUB_START_LOCK_PATH):
            time.sleep(0.5)
            try:
                self.health()
            except Exception:
                pass
            return
        try:
            try:
                self.health()
                return
            except Exception:
                pass
            subprocess.Popen(
                [sys.executable, str(HUB_SCRIPT), "--transport", self.transport],
                **process_kwargs(),
            )
            deadline = time.time() + 3.0
            while time.time() < deadline:
                try:
                    self.health()
                    return
                except Exception:
                    time.sleep(0.15)
        finally:
            release_start_lock(HUB_START_LOCK_PATH)

    def restart_hub(self) -> dict[str, Any]:
        try:
            return http_json(
                self.hub_url + "/module/restart",
                method="POST",
                body={"module": "hub"},
                timeout=2.0,
            )
        except Exception:
            self.ensure_hub()
            return {"ok": True, "message": "Hub start requested"}

    def restart_watcher(self) -> dict[str, Any]:
        try:
            return http_json(
                self.hub_url + "/module/restart",
                method="POST",
                body={"module": "codex-watcher"},
                timeout=3.0,
            )
        except Exception as exc:
            if not WATCHER_SCRIPT.exists():
                return {"ok": False, "error": "codex_session_watch.py not found"}
            subprocess.Popen([sys.executable, str(WATCHER_SCRIPT), "--follow-latest"], **process_kwargs())
            return {"ok": True, "message": f"watcher start requested after {exc}"}

    def ensure_watcher(self) -> None:
        try:
            modules = self.modules()
            watcher = modules.get("codex-watcher", {})
            if watcher.get("status") == "online":
                return
        except Exception:
            return
        self.restart_watcher()

    def open_dashboard(self) -> None:
        webbrowser.open(self.hub_url)


class HubApp:
    def __init__(self, controller: HubController, minimized: bool = False) -> None:
        self.controller = controller
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("560x440")
        self.root.minsize(480, 360)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_or_minimize)
        self.status_var = tk.StringVar(value="Starting Hub...")
        self.modules_var = tk.StringVar(value="")
        self.tray_icon: Any = None
        self.tray_available = False
        self.running = True
        self._build_ui()
        self._setup_tray()
        self.controller.ensure_hub()
        self.root.after(600, self.refresh)
        if minimized:
            self.hide_or_minimize()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text=APP_NAME, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.status_var).pack(anchor="w", pady=(4, 10))

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(0, 10))
        ttk.Button(buttons, text="Open Dashboard", command=self.controller.open_dashboard).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(buttons, text="Restart Hub", command=self.restart_hub).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(buttons, text="Restart Watcher", command=self.restart_watcher).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(buttons, text="Refresh", command=self.refresh).pack(side=tk.LEFT)

        self.tree = ttk.Treeview(frame, columns=("status", "detail"), show="headings", height=12)
        self.tree.heading("status", text="Status")
        self.tree.heading("detail", text="Detail")
        self.tree.column("status", width=130, anchor="w")
        self.tree.column("detail", width=340, anchor="w")
        self.tree.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Close hides to tray when pystray is installed; otherwise it minimizes.").pack(anchor="w", pady=(10, 0))

    def _setup_tray(self) -> None:
        try:
            import pystray  # type: ignore
            from PIL import Image, ImageDraw  # type: ignore
        except Exception:
            return

        image = Image.new("RGB", (64, 64), "#20242d")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((10, 10, 54, 54), radius=10, fill="#f2802e")
        draw.rectangle((20, 25, 44, 44), fill="#171a20")

        self.tray_icon = pystray.Icon(
            APP_NAME,
            image,
            APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("Show", lambda: self.root.after(0, self.show)),
                pystray.MenuItem("Open Dashboard", lambda: self.controller.open_dashboard()),
                pystray.MenuItem("Restart Hub", lambda: self.root.after(0, self.restart_hub)),
                pystray.MenuItem("Restart Watcher", lambda: self.root.after(0, self.restart_watcher)),
                pystray.MenuItem("Quit", lambda: self.root.after(0, self.quit)),
            ),
        )
        threading.Thread(target=self.tray_icon.run, daemon=True).start()
        self.tray_available = True

    def hide_or_minimize(self) -> None:
        if self.tray_available:
            self.root.withdraw()
        else:
            self.root.iconify()

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def restart_hub(self) -> None:
        result = self.controller.restart_hub()
        self.status_var.set(result.get("message") or json.dumps(result, ensure_ascii=False))
        self.root.after(2500, self.refresh)

    def restart_watcher(self) -> None:
        result = self.controller.restart_watcher()
        if result.get("ok"):
            self.status_var.set(f"Watcher restarted: {result.get('pid') or result.get('message') or 'ok'}")
        else:
            self.status_var.set(f"Watcher restart failed: {result.get('error')}")
        self.root.after(1000, self.refresh)

    def refresh(self) -> None:
        if not self.running:
            return
        self.controller.ensure_hub()
        try:
            modules = self.controller.modules()
            self.controller.ensure_watcher()
            self.status_var.set("Hub online")
            self.render_modules(modules)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            self.status_var.set(f"Hub starting or unavailable: {exc}")
        self.root.after(2500, self.refresh)

    def render_modules(self, modules: dict[str, Any]) -> None:
        self.tree.delete(*self.tree.get_children())
        for key, module in modules.items():
            label = module.get("label") or key
            status = module.get("status") or ""
            detail = module.get("detail") or ""
            self.tree.insert("", tk.END, values=(f"{label}: {status}", detail))

    def quit(self) -> None:
        self.running = False
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def status_json(controller: HubController) -> int:
    controller.ensure_hub()
    time.sleep(1.0)
    try:
        print(json.dumps(controller.modules(), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hub-url", default=os.environ.get("CLAWD_TANK_HUB_URL", DEFAULT_HUB_URL))
    parser.add_argument("--transport", default=os.environ.get("CLAWD_TANK_TRANSPORT", "auto"))
    parser.add_argument("--minimized", action="store_true")
    parser.add_argument("--status", action="store_true", help="start/check Hub and print module JSON")
    args = parser.parse_args()

    write_pid()
    controller = HubController(args.hub_url, args.transport)
    if args.status:
        return status_json(controller)
    app = HubApp(controller, minimized=args.minimized)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
