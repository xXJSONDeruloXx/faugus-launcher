"""Unprivileged client for UwU's CPUID compatibility backend."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

HELPER = Path("/usr/lib/uwu-launcher/hv_helper.py")
SOURCE_HELPER = Path(__file__).resolve().parent.parent / "hv" / "hv_helper.py"
RUNTIME_SOCKET = Path("/run/uwu-launcher/hv-runtime.sock")


class HvError(RuntimeError):
    pass


def helper_path():
    return HELPER if HELPER.is_file() else SOURCE_HELPER


def helper_command(action, privileged=False, values=()):
    command = ["/usr/bin/python3", str(helper_path()), "--backend", action, *map(str, values)]
    if not privileged:
        return command
    user = os.environ.get("USER", "")
    return [
        "pkexec", "env", "-i", f"SUDO_USER={user}",
        "PATH=/usr/sbin:/usr/bin:/sbin:/bin", *command,
    ]


def parse_status(output):
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)

    def enabled(key):
        return values.get(key) == "1"

    def number(key):
        try:
            return int(values.get(key, "0"))
        except ValueError:
            return 0

    return {
        "os": values.get("os", "linux"),
        "kernel": values.get("kernel", "unknown"),
        "arch": values.get("arch", "unknown"),
        "umip": values.get("umip", "unknown"),
        "umip_arg": values.get("umip_arg", "unknown"),
        "installed": enabled("installed"),
        "loaded": enabled("loaded"),
        "matching": enabled("matching"),
        "runtime_enabled": enabled("runtime_enabled"),
        "runtime_active": enabled("runtime_active"),
        "authorized_uid": values.get("authorized_uid", ""),
        "leases": number("leases"),
        "runtime_owns_module": enabled("runtime_owns_module"),
    }


def inspect(timeout=10):
    result = subprocess.run(
        helper_command("inspect"), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
    )
    if result.returncode:
        raise HvError(result.stdout.strip() or "Could not inspect CPUID compatibility")
    return parse_status(result.stdout)


def process_starttime(pid=None):
    pid = os.getpid() if pid is None else int(pid)
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError) as error:
        raise HvError("Could not identify the game runner process") from error


def runtime_request(request, timeout=30):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(RUNTIME_SOCKET))
            connection.sendall(json.dumps(request).encode("utf-8") + b"\n")
            raw = b""
            while b"\n" not in raw and len(raw) <= 65536:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                raw += chunk
    except (OSError, TimeoutError) as error:
        raise HvError(
            "Automatic CPUID compatibility is not ready. "
            "Open Settings → CPUID Compatibility Manager to finish setup."
        ) from error

    try:
        response = json.loads(raw.split(b"\n", 1)[0].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, IndexError) as error:
        raise HvError("The CPUID compatibility service returned an invalid response") from error
    if not response.get("ok"):
        raise HvError(response.get("error") or "CPUID compatibility activation failed")
    return response


class RuntimeLease:
    def __init__(self, game_id=""):
        self.game_id = str(game_id or "")
        self.token = None

    @property
    def active(self):
        return self.token is not None

    def acquire(self):
        if self.token:
            return self
        response = runtime_request({
            "action": "acquire",
            "pid": os.getpid(),
            "starttime": process_starttime(),
            "game_id": self.game_id,
        }, timeout=90)
        self.token = response["token"]
        return self

    def release(self):
        if not self.token:
            return
        token, self.token = self.token, None
        try:
            runtime_request({"action": "release", "token": token}, timeout=30)
        except HvError as error:
            # The root service also reaps this lease when the runner exits.
            print(f"CPUID compatibility cleanup deferred: {error}", file=sys.stderr)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_args):
        self.release()
