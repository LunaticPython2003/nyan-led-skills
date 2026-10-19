"""Install and control a per-user systemd watcher; no root service is needed."""

from pathlib import Path
import subprocess
import sys

UNIT = "nyan-pack.service"


def unit_text(python):
    def escape(value):
        value = str(value)
        if any(character in value for character in "\n\r"):
            raise ValueError("Invalid service path")
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    value = escape(python)
    executable_path = escape(f"{Path(python).parent}:{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin")
    return f'''[Unit]
Description=Nyan BLE display plugin watcher
After=graphical-session.target

[Service]
Type=simple
ExecStart="{value}" -u -m nyan_pack watch
Environment=PYTHONUNBUFFERED=1
Environment="PATH={executable_path}"
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
KillMode=control-group

[Install]
WantedBy=default.target
'''


def control(action):
    if action == "logs":
        return subprocess.run(["journalctl", "--user", "-u", UNIT, "-n", "50", "--no-pager"]).returncode
    if action == "install":
        directory = Path.home() / ".config/systemd/user"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / UNIT
        destination.write_text(unit_text(Path(sys.executable).absolute()))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", UNIT], check=True)
        launcher = Path.home() / ".local/bin/nyan"
        target = Path(sys.executable).absolute().parent / "nyan"
        if target.exists() and not launcher.exists() and not launcher.is_symlink():
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.symlink_to(target)
        print(f"Installed and started {destination}")
        return 0
    return subprocess.run(["systemctl", "--user", action, UNIT]).returncode
