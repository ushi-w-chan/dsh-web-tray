#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-web-tray: install / uninstall the DeepSeek Harness tray app.

Portable installer for Debian-like systems. It installs a small GTK3 +
AyatanaAppIndicator tray application that runs "dsh web" in the background,
shows DeepSeek's whale icon in the system tray, and lets the menu open the web
UI, restart/stop the server, or quit.

Usage:
    python3 install.py                 # install (asks sudo for packages/*/usr/local icons)
    python3 install.py --port 3081     # install with another listen port
    python3 install.py --no-apt        # skip apt-get install
    python3 install.py --no-system-icons
    python3 install.py --autostart     # also start at login
    python3 install.py --start         # launch right after install
    python3 install.py --uninstall     # remove everything (keeps config unless --purge)
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP = "dsh-web-tray"
ICON_NAME = "deepseek-whale"
SIZES = (16, 22, 24, 32, 48, 64, 128, 256)
APT_PACKAGES = [
    "python3-gi",
    "gir1.2-gtk-3.0",
    "gir1.2-ayatanaappindicator3-0.1",
    "librsvg2-bin",
    "iproute2",
]

HOME = Path.home()
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share")
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local/state")

DATA_DIR = DATA_HOME / APP
ICON_ROOT = DATA_HOME / "icons/hicolor"
SYS_ICON_ROOT = Path("/usr/local/share/icons/hicolor")
BIN_PATH = HOME / ".local/bin/dsh-web-tray"
DESKTOP_PATH = DATA_HOME / "applications/dsh-web.desktop"
AUTOSTART_PATH = CONFIG_HOME / "autostart/dsh-web.desktop"
CONFIG_PATH = CONFIG_HOME / APP / "config.json"
STATE_DIR = STATE_HOME / APP

HERE = Path(__file__).resolve().parent
SRC_APP = HERE / "files" / "dsh_web_tray.py"
SRC_SVG = HERE / "files" / "dsh-web-whale.svg"


def say(message):
    print("[dsh-web-tray] " + message)


def warn(message):
    print("[dsh-web-tray] WARN: " + message)


def run(args, check=True, env=None):
    say("$ " + " ".join(str(a) for a in args))
    return subprocess.run([str(a) for a in args], check=check, env=env)


def as_root(args):
    if os.geteuid() == 0:
        return args
    return ["sudo"] + list(args)


def apt_install():
    if not shutil.which("apt-get"):
        warn("no apt-get found, skipping package installation")
        return
    run(as_root(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
                 "--no-install-recommends"] + APT_PACKAGES))


def have_module(module_name):
    code = (
        "import gi\n"
        "gi.require_version('Gtk', '3.0')\n"
        "gi.require_version('AyatanaAppIndicator3', '0.1')\n"
        "from gi.repository import Gtk, AyatanaAppIndicator3\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr.strip().splitlines() or ["unknown error"])[-1]
        warn("python bindings incomplete: " + detail)
    return result.returncode == 0


def install_icons(user_icon_root, system_icon_root, with_system):
    say("installing icons into " + str(user_icon_root))
    for size in SIZES:
        directory = user_icon_root / (str(size) + "x" + str(size)) / "apps"
        directory.mkdir(parents=True, exist_ok=True)
        source = HERE / "files" / "png" / ("deepseek-whale-" + str(size) + ".png")
        target = directory / (ICON_NAME + ".png")
        if source.exists():
            shutil.copy2(source, target)
        else:
            run(["rsvg-convert", "-w", str(size), "-h", str(size), SRC_SVG, "-o", target])
    scalable = user_icon_root / "scalable" / "apps"
    scalable.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SRC_SVG, scalable / (ICON_NAME + ".svg"))

    if not with_system:
        return
    say("installing icons into " + str(system_icon_root) + " (Qt/DMS only scans system theme dirs)")
    for size in SIZES:
        directory = system_icon_root / (str(size) + "x" + str(size)) / "apps"
        run(as_root(["mkdir", "-p", directory]))
        run(as_root(["cp", user_icon_root / (str(size) + "x" + str(size)) / "apps" / (ICON_NAME + ".png"),
                     directory / (ICON_NAME + ".png")]))
    scalable_sys = system_icon_root / "scalable" / "apps"
    run(as_root(["mkdir", "-p", scalable_sys]))
    run(as_root(["cp", SRC_SVG, scalable_sys / (ICON_NAME + ".svg")]))
    cache = shutil.which("gtk-update-icon-cache")
    if cache:
        run(as_root([cache, "-f", "-t", system_icon_root]), check=False)


def write_app_files():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SRC_APP, DATA_DIR / "dsh_web_tray.py")
    os.chmod(DATA_DIR / "dsh_web_tray.py", 0o755)
    shutil.copy2(SRC_SVG, DATA_DIR / "dsh-web-whale.svg")
    shutil.copy2(HERE / "files" / "png" / "deepseek-whale-64.png", DATA_DIR / "dsh-web-whale-64.png")

    BIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    BIN_PATH.write_text(
        "#!/bin/sh\n"
        "# dsh-web-tray launcher\n"
        "exec /usr/bin/python3 \"$HOME/.local/share/dsh-web-tray/dsh_web_tray.py\" \"$@\"\n",
        encoding="utf-8",
    )
    os.chmod(BIN_PATH, 0o755)


def write_desktop():
    DESKTOP_PATH.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP_PATH.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.1\n"
        "Name=DeepSeek Harness\n"
        "GenericName=AI Coding Agent\n"
        "GenericName[zh_CN]=AI 编程助手\n"
        "Comment=Run the DeepSeek Harness web UI in the background with a tray icon\n"
        "Comment[zh_CN]=在后台运行 DeepSeek Harness Web 界面，并在系统托盘常驻\n"
        "Keywords=deepseek;harness;dsh;ai;agent;web;tray;whale;assistant;深度求索;智能体;编程助手;鲸鱼;\n"
        "Exec=" + str(BIN_PATH) + "\n"
        "Icon=" + ICON_NAME + "\n"
        "Terminal=false\n"
        "Categories=Development;\n"
        "StartupNotify=false\n"
        "StartupWMClass=" + APP + "\n",
        encoding="utf-8",
    )
    os.chmod(DESKTOP_PATH, 0o644)
    updater = shutil.which("update-desktop-database")
    if updater:
        run([updater, str(DESKTOP_PATH.parent)], check=False)
    validator = shutil.which("desktop-file-validate")
    if validator:
        run([validator, str(DESKTOP_PATH)], check=False)


def write_config(port):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        say("keeping existing config " + str(CONFIG_PATH))
        return
    CONFIG_PATH.write_text(
        "{\n"
        "  \"port\": " + str(port) + ",\n"
        "  \"host\": \"127.0.0.1\",\n"
        "  \"command\": \"\",\n"
        "  \"auto_restart\": true,\n"
        "  \"open_browser_on_launch\": false,\n"
        "  \"extra_args\": []\n"
        "}\n",
        encoding="utf-8",
    )
    os.chmod(CONFIG_PATH, 0o644)


def install_autostart():
    AUTOSTART_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DESKTOP_PATH, AUTOSTART_PATH)
    say("autostart entry written to " + str(AUTOSTART_PATH))


def environment_checks():
    ok = True
    if not have_module("gi"):
        warn("python3-gi / GTK3 / AppIndicator imports failed; run without --no-apt")
        ok = False
    dsh = shutil.which("dsh")
    npx_cache = list((HOME / ".npm/_npx").glob("*/node_modules/@deepseek-ai/dsh/lib/bin.js"))
    if dsh:
        say("dsh found: " + dsh)
    elif npx_cache:
        say("dsh found in the npx cache: " + str(sorted(npx_cache)[-1]))
    elif shutil.which("npx"):
        warn("dsh not installed; the app will fall back to: npx -y @deepseek-ai/dsh web "
             "(slow first start). Prefer: npm install -g @deepseek-ai/dsh")
    else:
        warn("no dsh, no npx: install Node.js first (nvm or apt), then npm install -g @deepseek-ai/dsh")
        ok = False

    busctl = shutil.which("busctl")
    if busctl:
        out = subprocess.run([busctl, "--user", "list"], capture_output=True, text=True)
        if "StatusNotifierWatcher" in (out.stdout or ""):
            say("tray host found (StatusNotifierWatcher present on the session bus)")
        else:
            warn("no StatusNotifierWatcher on the session bus: the icon will not show. "
                 "The desktop shell needs a tray (DMS/quickshell, Waybar with tray module, KDE Plasma...).")
    return ok


def uninstall(purge):
    if BIN_PATH.exists():
        subprocess.run([BIN_PATH, "--stop"], check=False)
    for path in (BIN_PATH, DESKTOP_PATH, AUTOSTART_PATH):
        if path.exists():
            path.unlink()
            say("removed " + str(path))
    for directory in (DATA_DIR, STATE_DIR):
        if directory.exists():
            shutil.rmtree(directory)
            say("removed " + str(directory))
    for size in SIZES:
        for root in (ICON_ROOT, SYS_ICON_ROOT):
            target = root / (str(size) + "x" + str(size)) / "apps" / (ICON_NAME + ".png")
            if target.exists():
                if str(root).startswith("/usr"):
                    run(as_root(["rm", "-f", target]), check=False)
                else:
                    target.unlink()
    for root in (ICON_ROOT, SYS_ICON_ROOT):
        target = root / "scalable" / "apps" / (ICON_NAME + ".svg")
        if target.exists():
            if str(root).startswith("/usr"):
                run(as_root(["rm", "-f", target]), check=False)
            else:
                target.unlink()
    if purge and CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
        say("removed " + str(CONFIG_PATH))
    say("uninstalled")


def main():
    parser = argparse.ArgumentParser(description="Install the DeepSeek Harness tray application.")
    parser.add_argument("--port", type=int, default=3080, help="listen port (default 3080)")
    parser.add_argument("--no-apt", action="store_true", help="do not run apt-get install")
    parser.add_argument("--no-system-icons", action="store_true",
                        help="skip /usr/local/share/icons (launcher icon may fall back to a letter)")
    parser.add_argument("--autostart", action="store_true", help="also start the tray at login")
    parser.add_argument("--start", action="store_true", help="launch the tray after installing")
    parser.add_argument("--uninstall", action="store_true", help="remove the application")
    parser.add_argument("--purge", action="store_true", help="with --uninstall: also delete config.json")
    args = parser.parse_args()

    if not SRC_APP.exists() or not SRC_SVG.exists():
        raise SystemExit("broken package: files/dsh_web_tray.py and files/dsh-web-whale.svg are required")

    if args.uninstall:
        uninstall(args.purge)
        return 0

    if not args.no_apt:
        apt_install()

    say("installing application files")
    write_app_files()
    install_icons(ICON_ROOT, SYS_ICON_ROOT, not args.no_system_icons)
    write_desktop()
    write_config(args.port)
    if args.autostart:
        install_autostart()

    cache = shutil.which("gtk-update-icon-cache")
    if cache and ICON_ROOT.exists():
        run([cache, "-f", "-t", str(ICON_ROOT)], check=False)

    print("")
    ok = environment_checks()
    print("")
    say("installed:")
    say("  app      " + str(DATA_DIR / "dsh_web_tray.py"))
    say("  launcher " + str(BIN_PATH))
    say("  entry    " + str(DESKTOP_PATH))
    say("  config   " + str(CONFIG_PATH))
    say("  logs     " + str(STATE_DIR / "server.log"))
    print("")
    say("next steps:")
    say("  1. open your launcher (Super/Win + Space) and search for 'DeepSeek Harness'")
    say("  2. or run: " + str(BIN_PATH))
    if not ok:
        warn("some checks failed, see the warnings above")

    if args.start:
        subprocess.Popen([str(BIN_PATH)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
        say("tray started")
    return 0


if __name__ == "__main__":
    sys.exit(main())
