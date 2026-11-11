#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-web-tray: run the DeepSeek Harness web server in the background with a
tray icon.

Launched from the application launcher, it starts 'dsh web --no-open' as a
child process and shows a StatusNotifierItem in the desktop tray. The menu can
open the browser UI, copy the URL, restart or stop the server, open the log,
and quit (which stops the server it started).

CLI:
  dsh-web-tray            start the tray (or open the UI if already running)
  dsh-web-tray --status   print the running instance
  dsh-web-tray --stop     stop a running tray instance
  dsh-web-tray --open     open the web UI
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

APP_ID = "dsh-web-tray"
APP_NAME = "DeepSeek Harness"
ICON_NAME = "deepseek-whale"
VERSION = "1.0.0"

HOME = Path.home()


def _xdg(var: str, default: Path) -> Path:
    return Path(os.environ.get(var) or default)


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", HOME / ".config") / APP_ID
STATE_DIR = _xdg("XDG_STATE_HOME", HOME / ".local/state") / APP_ID
DATA_DIR = _xdg("XDG_DATA_HOME", HOME / ".local/share") / APP_ID
CONFIG_FILE = CONFIG_DIR / "config.json"
LOCK_FILE = STATE_DIR / "instance.lock"
PID_FILE = STATE_DIR / "instance.pid"
SERVER_LOG = STATE_DIR / "server.log"

DEFAULT_CONFIG = {
    "port": 3080,
    "host": "127.0.0.1",
    "command": "",
    "auto_restart": True,
    "open_browser_on_launch": False,
    "extra_args": [],
}

START_TIMEOUT = 60.0
TICK_MS = 1200
MAX_LOG_BYTES = 5 * 1024 * 1024
MAX_RECOVERIES = 5
RECOVERY_WINDOW = 180.0


def ensure_dirs() -> None:
    for directory in (CONFIG_DIR, STATE_DIR, DATA_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key, value in raw.items():
                if key in DEFAULT_CONFIG:
                    cfg[key] = value
    except Exception:
        pass
    try:
        cfg["port"] = int(cfg["port"])
    except Exception:
        cfg["port"] = DEFAULT_CONFIG["port"]
    if not (0 < cfg["port"] < 65536):
        cfg["port"] = DEFAULT_CONFIG["port"]
    return cfg


def augment_path() -> None:
    """Make node, npx and dsh resolvable even when launched from a .desktop file."""
    dirs: list[str] = []
    nvm = HOME / ".nvm" / "versions" / "node"
    if nvm.is_dir():
        for entry in sorted(nvm.glob("*/bin"), key=lambda p: p.stat().st_mtime, reverse=True):
            dirs.append(str(entry))
    dirs += [str(HOME / ".local/bin"), "/usr/local/bin", "/usr/bin", "/bin"]
    current = [p for p in os.environ.get("PATH", "").split(":") if p]
    merged: list[str] = []
    for item in dirs + current:
        if item not in merged:
            merged.append(item)
    os.environ["PATH"] = ":".join(merged)


def resolve_command(cfg: dict) -> list[str]:
    if cfg.get("command"):
        return shlex.split(str(cfg["command"]))
    base: list[str]
    dsh = shutil.which("dsh")
    if dsh:
        base = [dsh]
    else:
        node = shutil.which("node")
        cache = HOME / ".npm" / "_npx"
        candidates = sorted(
            cache.glob("*/node_modules/@deepseek-ai/dsh/lib/bin.js"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ) if cache.is_dir() else []
        if node and candidates:
            base = [node, str(candidates[0])]
        else:
            npx = shutil.which("npx")
            if not npx:
                raise RuntimeError("找不到 dsh 或 npx，请确认 node 已安装")
            base = [npx, "--yes", "@deepseek-ai/dsh"]
    return base + [
        "web",
        "--no-open",
        "--host", str(cfg["host"]),
        "--port", str(cfg["port"]),
    ] + [str(arg) for arg in cfg.get("extra_args") or []]


def icon_spec() -> str:
    """Tray hosts differ: some resolve a theme icon name, others need a file."""
    for candidate in (
        DATA_DIR / "dsh-web-whale-64.png",
        DATA_DIR / "dsh-web-64.png",
        DATA_DIR / "dsh-web.svg",
    ):
        if candidate.exists():
            return str(candidate)
    return ICON_NAME


def port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def spawn_detached(argv: list[str]) -> None:
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def notify(summary: str, body: str = "") -> None:
    """Best-effort desktop notification over org.freedesktop.Notifications."""
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "Notify",
            GLib.Variant("(susssasa{sv}i)", (
                APP_NAME, 0, ICON_NAME, summary, body, [], {}, 6000,
            )),
            None, Gio.DBusCallFlags.NONE, 2000, None,
        )
    except Exception:
        pass


class ServerManager:
    """Owns the dsh web child process and the state shown in the menu."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.state = "stopped"   # stopped | starting | running | external | failed
        self.detail = ""
        self.started_at = 0.0
        self.recoveries: deque[float] = deque(maxlen=MAX_RECOVERIES + 1)
        self.recovery_at = 0.0
        self.paused = False
        self.closing = False
        self._log_handle = None
        self.url = "http://%s:%d/" % (cfg["host"], cfg["port"])

    # -- helpers ---------------------------------------------------------
    def port_open(self) -> bool:
        return port_open(self.cfg["host"], self.cfg["port"])

    def _open_log(self):
        try:
            if SERVER_LOG.exists() and SERVER_LOG.stat().st_size > MAX_LOG_BYTES:
                os.replace(SERVER_LOG, SERVER_LOG.with_suffix(".log.1"))
        except OSError:
            pass
        handle = open(SERVER_LOG, "ab", buffering=0)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        handle.write(("\n=== %s  dsh-web-tray start ===\n" % stamp).encode())
        return handle

    def _learn_url(self) -> None:
        """Adopt the URL the server printed, which may carry an access token."""
        try:
            with open(SERVER_LOG, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 8192))
                tail = handle.read().decode("utf-8", "replace")
        except OSError:
            return
        needle = ":%d/" % self.cfg["port"]
        found = [match for match in re.findall(r"https?://[^\s\"'<>]+", tail) if needle in match]
        if found:
            self.url = found[-1]

    # -- lifecycle -------------------------------------------------------
    def start(self, reason: str = "") -> bool:
        if self.proc is not None and self.proc.poll() is None:
            return False
        if self.port_open():
            self.state = "external"
            self.detail = "端口 %d 已被其他进程占用" % self.cfg["port"]
            return False
        try:
            command = resolve_command(self.cfg)
        except Exception as error:
            self.state = "failed"
            self.detail = str(error)
            notify("DeepSeek Harness 无法启动", str(error))
            return False
        self._log_handle = self._open_log()
        try:
            self.proc = subprocess.Popen(
                command,
                cwd=str(HOME),
                stdin=subprocess.DEVNULL,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=os.environ.copy(),
            )
        except Exception as error:
            self.state = "failed"
            self.detail = "启动失败：%s" % error
            notify("DeepSeek Harness 无法启动", self.detail)
            return False
        self.state = "starting"
        self.detail = reason or "正在启动服务…"
        self.started_at = time.monotonic()
        self.paused = False
        return True

    def _signal_group(self, sig: int) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), sig)
        except OSError:
            try:
                self.proc.send_signal(sig)
            except OSError:
                pass

    def stop(self, grace: float = 10.0) -> None:
        self._signal_group(signal.SIGTERM)
        if self.proc is not None:
            try:
                self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self._signal_group(signal.SIGKILL)
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self.proc = None
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None

    def stop_manual(self) -> None:
        self.paused = True
        self.stop()
        self.state = "stopped"
        self.detail = "已手动停止"

    def restart(self) -> None:
        self.paused = False
        self.stop()
        self.state = "stopped"
        self.start("正在重启服务…")

    def shutdown(self) -> None:
        self.closing = True
        self.paused = True
        self.stop()

    def _listener_pid(self):
        """PID of the process listening on our port, if we can see it."""
        try:
            out = subprocess.run(
                ["ss", "-ltnpH", "sport = :%d" % self.cfg["port"]],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:
            return None
        match = re.search(r"pid=(\d+)", out)
        return int(match.group(1)) if match else None

    def take_over(self) -> bool:
        """Stop the process holding the port, then start our own server."""
        pid = self._listener_pid()
        if pid is None:
            self.start("正在启动服务…")
            return True
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as handle:
                cmdline = handle.read().decode("utf-8", "replace")
        except OSError:
            cmdline = ""
        if "dsh" not in cmdline or "web" not in cmdline:
            notify(APP_NAME, "端口由非 DSH 进程占用，已放弃接管")
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as error:
            notify(APP_NAME, "无法停止占用端口的进程：%s" % error)
            return False
        for _ in range(100):
            if not self.port_open():
                break
            time.sleep(0.1)
        self.paused = False
        self.state = "stopped"
        self.detail = "正在接管服务…"
        return self.start("正在接管服务…")

    def _schedule_recovery(self) -> None:
        now = time.monotonic()
        while self.recoveries and now - self.recoveries[0] > RECOVERY_WINDOW:
            self.recoveries.popleft()
        if len(self.recoveries) >= MAX_RECOVERIES:
            self.state = "failed"
            self.detail = "自动重启次数过多，已停止重试（见日志）"
            notify("DeepSeek Harness 已停止", self.detail)
            return
        self.recoveries.append(now)
        self.recovery_at = now + 3.0

    # -- status ----------------------------------------------------------
    def refresh(self) -> bool:
        previous = (self.state, self.detail)
        now = time.monotonic()

        if self.proc is not None and self.proc.poll() is not None:
            code = self.proc.returncode
            self.proc = None
            self.state = "stopped"
            self.detail = "服务已退出（exit %s）" % code
            if not self.closing and not self.paused and self.cfg.get("auto_restart"):
                self._schedule_recovery()

        if self.proc is not None:
            if self.port_open():
                if self.state != "running":
                    self.detail = ""
                self.state = "running"
                self._learn_url()
            elif self.state != "starting":
                self.state = "starting"
                self.detail = "等待端口就绪…"
                self.started_at = now
            elif now - self.started_at > START_TIMEOUT:
                self.state = "failed"
                self.detail = "启动超时，请查看日志"
                self.stop(grace=2.0)
                self.paused = True
        else:
            if self.port_open():
                if self.state != "external":
                    self.state = "external"
                    self.detail = "端口被其他进程占用（外部实例）"
            elif self.state in ("running", "external"):
                self.state = "stopped"
                self.detail = "服务已不可用"
                if not self.closing and not self.paused and self.cfg.get("auto_restart"):
                    self._schedule_recovery()

        if self.recovery_at and now >= self.recovery_at:
            self.recovery_at = 0.0
            if not self.closing and not self.paused and self.proc is None and not self.port_open():
                self.start("正在重启服务…")

        return previous != (self.state, self.detail)

    def header(self) -> str:
        return {
            "running": APP_NAME + " · 运行中",
            "starting": APP_NAME + " · 启动中",
            "external": APP_NAME + " · 外部实例",
            "failed": APP_NAME + " · 异常",
            "stopped": APP_NAME + " · 已停止",
        }.get(self.state, APP_NAME)


class TrayApp:
    def __init__(self, cfg: dict) -> None:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AppIndicator
        from gi.repository import GLib, Gtk

        self.GLib = GLib
        self.Gtk = Gtk
        self.cfg = cfg
        self.manager = ServerManager(cfg)
        self.menu = None

        self.indicator = AppIndicator.Indicator.new(
            APP_ID, icon_spec(), AppIndicator.IndicatorCategory.APPLICATION_STATUS
        )
        self.indicator.set_title(APP_NAME)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.connect("scroll-event", lambda *_: False)

        self._pending_open = bool(cfg.get("open_browser_on_launch"))
        self._build_menu()
        self.manager.start()
        self.manager.refresh()
        self._sync()
        self._install_signals()

    # -- menu ------------------------------------------------------------
    def _build_menu(self):
        Gtk = self.Gtk
        menu = Gtk.Menu()

        self.title_item = Gtk.MenuItem(label=self.manager.header())
        self.title_item.set_sensitive(False)
        menu.append(self.title_item)

        self.detail_item = Gtk.MenuItem(label=self.manager.url)
        self.detail_item.set_sensitive(False)
        menu.append(self.detail_item)

        menu.append(Gtk.SeparatorMenuItem())

        self.open_item = Gtk.MenuItem(label="打开 Web 界面")
        self.open_item.connect("activate", self.open_web)
        menu.append(self.open_item)

        copy_item = Gtk.MenuItem(label="复制访问地址")
        copy_item.connect("activate", self.copy_url)
        menu.append(copy_item)

        menu.append(Gtk.SeparatorMenuItem())

        self.restart_item = Gtk.MenuItem(label="重启服务")
        self.restart_item.connect("activate", self.restart)
        menu.append(self.restart_item)

        self.stop_item = Gtk.MenuItem(label="停止服务")
        self.stop_item.connect("activate", self.stop_service)
        menu.append(self.stop_item)

        self.takeover_item = Gtk.MenuItem(label="接管服务并重启（会断开当前页面）")
        self.takeover_item.connect("activate", self.take_over)
        menu.append(self.takeover_item)

        log_item = Gtk.MenuItem(label="打开日志")
        log_item.connect("activate", self.open_log)
        menu.append(log_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="退出")
        quit_item.connect("activate", self.quit)
        menu.append(quit_item)

        menu.show_all()
        self.menu = menu
        self.indicator.set_menu(menu)
        # middle / secondary click activates that item (SNI SecondaryActivate)
        try:
            self.indicator.set_secondary_activate_target(self.open_item)
        except Exception:
            pass
        return menu

    def _sync(self) -> None:
        state = self.manager.state
        self.title_item.set_label(self.manager.header())
        self.detail_item.set_label(self.manager.detail or self.manager.url)
        self.indicator.set_title("%s — %s" % (self.manager.header(), self.manager.url))

        self.takeover_item.set_visible(state == "external")
        if state == "external":
            self.restart_item.set_label("重启服务（外部实例占用）")
            self.restart_item.set_sensitive(False)
            self.stop_item.set_sensitive(False)
        else:
            self.restart_item.set_label("重启服务")
            self.restart_item.set_sensitive(True)
            self.stop_item.set_sensitive(self.manager.proc is not None or state in ("starting", "running"))

    # -- actions ---------------------------------------------------------
    def open_web(self, *_args) -> None:
        spawn_detached(["xdg-open", self.manager.url])

    def copy_url(self, *_args) -> None:
        from gi.repository import Gdk
        clipboard = self.Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(self.manager.url, -1)
        clipboard.store()
        notify(APP_NAME, "已复制 %s" % self.manager.url)

    def restart(self, *_args) -> None:
        self.manager.restart()
        self.manager.refresh()
        self._sync()

    def take_over(self, *_args) -> None:
        self.manager.take_over()
        self.manager.refresh()
        self._sync()

    def stop_service(self, *_args) -> None:
        self.manager.stop_manual()
        self._sync()

    def open_log(self, *_args) -> None:
        if not SERVER_LOG.exists():
            SERVER_LOG.touch()
        spawn_detached(["xdg-open", str(SERVER_LOG)])

    def _on_activate(self, *_args) -> None:
        self.open_web()

    def quit(self, *_args) -> None:
        self.manager.shutdown()
        for path in (PID_FILE, LOCK_FILE):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self.Gtk.main_quit()

    def _install_signals(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            self.GLib.unix_signal_add(self.GLib.PRIORITY_DEFAULT, sig, self.quit)

    # -- mainloop --------------------------------------------------------
    def tick(self) -> bool:
        if self.manager.refresh():
            self._sync()
        if self._pending_open and self.manager.state in ("running", "external"):
            self._pending_open = False
            self.open_web()
        return True

    def run(self) -> None:
        self.GLib.timeout_add(TICK_MS, self.tick)
        self.Gtk.main()


def acquire_lock():
    handle = open(LOCK_FILE, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


def running_pid():
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def cmd_status(cfg: dict) -> int:
    pid = running_pid()
    if pid:
        print("%s: 运行中 (pid %d) — %s" % (APP_ID, pid, "http://%s:%d/" % (cfg["host"], cfg["port"])))
        return 0
    print("%s: 未运行" % APP_ID)
    return 1


def cmd_stop() -> int:
    pid = running_pid()
    if not pid:
        print("%s: 未运行" % APP_ID)
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as error:
        print("无法停止 pid %d: %s" % (pid, error), file=sys.stderr)
        return 1
    for _ in range(120):
        if running_pid() is None:
            break
        time.sleep(0.1)
    print("%s: 已停止" % APP_ID)
    return 0


def cmd_open(cfg: dict) -> int:
    spawn_detached(["xdg-open", "http://%s:%d/" % (cfg["host"], cfg["port"])])
    return 0


def main(argv: list[str]) -> int:
    ensure_dirs()
    augment_path()
    cfg = load_config()

    flag = argv[1] if len(argv) > 1 else ""
    if flag in ("--status", "-s"):
        return cmd_status(cfg)
    if flag in ("--stop", "-q"):
        return cmd_stop()
    if flag in ("--open", "-o"):
        return cmd_open(cfg)
    if flag in ("--version", "-V"):
        print(VERSION)
        return 0
    if flag in ("--help", "-h"):
        print(__doc__)
        return 0

    lock = acquire_lock()
    if lock is None:
        spawn_detached(["xdg-open", "http://%s:%d/" % (cfg["host"], cfg["port"])])
        return 0

    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    app = None
    try:
        app = TrayApp(cfg)
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        if app is not None:
            try:
                app.manager.shutdown()
            except Exception:
                pass
        for path in (PID_FILE, LOCK_FILE):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
