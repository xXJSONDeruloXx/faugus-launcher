#!/usr/bin/env python3
"""Privileged CPUID Fault Emulation backend and hosted-game lease service.

The GTK launcher never runs writable application code as root.  Administrative
operations enter through this package-owned helper, while normal game launches
use a root service that authenticates its one configured desktop user via Unix
socket credentials.
"""

from __future__ import annotations

import ctypes
import fcntl
import getpass
import json
import mmap
import os
import pwd
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

STATE_DIR = Path("/var/lib/uwu-launcher")
SOURCE_DIR = STATE_DIR / "source"
MODULE_FILE = STATE_DIR / "cpuid_fault_emulation.ko"
AUTHORIZED_UID_FILE = STATE_DIR / "authorized-uid"
METADATA_FILE = STATE_DIR / "integration.json"

INSTALLED_ROOT = Path("/usr/lib/uwu-launcher")
INSTALLED_SOURCE = INSTALLED_ROOT / "cpuid_fault_emulation"
SERVICE_NAME = "uwu-hv-runtime.service"
LEGACY_SERVICE_NAME = "hv-games.service"

RUNTIME_DIR = Path("/run/uwu-launcher")
RUNTIME_SOCKET = RUNTIME_DIR / "hv-runtime.sock"
RUNTIME_STATE = RUNTIME_DIR / "runtime-state.json"
RUNTIME_OWNS_MODULE = RUNTIME_DIR / "owns-module"

# Shared with HV Installer GTK so both tools serialize the same global module
# and restore the same KVM modules.
MODULE_LOCK = Path("/run/hv-installer.lock")
KVM_STATE = Path("/run/hv-installer-kvm-modules")

ACTIONS = {
    "inspect": False,
    "install": True,
    "update": True,
    "uninstall": True,
    "start": True,
    "stop": True,
    "enable_runtime": True,
    "disable_runtime": True,
    "disable_umip": True,
    "enable_umip": True,
    "disable_umip_entry": True,
    "enable_umip_entry": True,
    "bootloader": False,
    "reboot": True,
}


class BackendError(RuntimeError):
    pass


def quiet(args, **kwargs):
    try:
        return subprocess.run(
            [str(part) for part in args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
    except OSError:
        return subprocess.CompletedProcess(args, 127, "", "")


def desktop_user():
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or getpass.getuser()


def desktop_account():
    name = desktop_user()
    if name == "root":
        sudo_uid = os.environ.get("SUDO_UID") or os.environ.get("PKEXEC_UID")
        if sudo_uid and sudo_uid.isdigit():
            return pwd.getpwuid(int(sudo_uid))
    return pwd.getpwnam(name)


def as_desktop_user(args):
    if os.geteuid() != 0:
        return [str(part) for part in args]
    account = desktop_account()
    if account.pw_uid == 0:
        return [str(part) for part in args]
    runtime = Path(f"/run/user/{account.pw_uid}")
    if not runtime.is_dir():
        runtime = Path(f"/tmp/uwu-podman-runtime-{account.pw_uid}")
        runtime.mkdir(mode=0o700, exist_ok=True)
        os.chown(runtime, account.pw_uid, account.pw_gid)
    return [
        "runuser", "-u", account.pw_name, "--", "env",
        f"HOME={account.pw_dir}", f"XDG_RUNTIME_DIR={runtime}",
        *[str(part) for part in args],
    ]


def run(args, cwd=None, user=False, env=None):
    argv = as_desktop_user(args) if user else [str(part) for part in args]
    print("+", " ".join(argv), flush=True)
    result = subprocess.run(argv, cwd=cwd, env=env)
    if result.returncode:
        raise BackendError(f"Command failed with status {result.returncode}: {argv[0]}")
    return result


def gaming_os():
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8").lower()
    except OSError:
        return "linux"
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    os_id = values.get("id", "").strip('"')
    variant = values.get("variant_id", "").strip('"')
    names = values.get("name", "") + values.get("pretty_name", "")
    if os_id == "bazzite" or variant == "bazzite":
        return "bazzite"
    if os_id == "steamos" or variant == "steamdeck" or "steamos" in names:
        return "steamos"
    return "linux"


def local_module():
    return gaming_os() in {"bazzite", "steamos"}


def kernel_module_loaded(name):
    try:
        return any(
            line.startswith(name + " ")
            for line in Path("/proc/modules").read_text(encoding="utf-8").splitlines()
        )
    except OSError:
        return False


def module_loaded():
    return kernel_module_loaded("cpuid_fault_emulation")


def module_target():
    return str(MODULE_FILE) if local_module() and MODULE_FILE.is_file() else "cpuid_fault_emulation"


def module_installed():
    if local_module() and MODULE_FILE.is_file():
        return True
    return quiet(["modinfo", "cpuid_fault_emulation"]).returncode == 0


def module_matches():
    if not module_installed():
        return False
    result = quiet(["modinfo", "-F", "vermagic", module_target()])
    if result.returncode:
        return False
    return result.stdout.split(" ", 1)[0].strip() == os.uname().release


def systemctl_state(kind, unit):
    if not shutil.which("systemctl"):
        return False
    return quiet(["systemctl", f"is-{kind}", "--quiet", unit]).returncode == 0


def runtime_snapshot():
    try:
        data = json.loads(RUNTIME_STATE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def authorized_uid():
    try:
        value = AUTHORIZED_UID_FILE.read_text(encoding="ascii").strip()
        return int(value) if value.isdigit() else None
    except OSError:
        return None


def clearcpuid_configured():
    try:
        if gaming_os() == "bazzite" and shutil.which("rpm-ostree"):
            return "clearcpuid=514" in quiet(["rpm-ostree", "kargs"]).stdout.split()
        kind = bootloader()
        if kind == "limine":
            paths = [Path("/etc/default/limine")]
        elif kind == "grub":
            paths = [Path("/etc/default/grub")]
        else:
            paths = list(Path("/boot/loader/entries").glob("*.conf"))
        return any("clearcpuid=514" in path.read_text(encoding="utf-8") for path in paths)
    except (OSError, BackendError):
        return False


def inspect_values():
    try:
        cpu = Path("/proc/cpuinfo").read_text(encoding="utf-8").lower()
    except OSError:
        cpu = ""
    runtime = runtime_snapshot()
    return {
        "os": gaming_os(),
        "kernel": os.uname().release,
        "arch": os.uname().machine,
        "umip": "enabled" if re.search(r"\bumip\b", cpu) else "disabled",
        "umip_arg": "present" if clearcpuid_configured() else "absent",
        "installed": int(module_installed()),
        "loaded": int(module_loaded()),
        "matching": int(module_matches()),
        "runtime_enabled": int(systemctl_state("enabled", SERVICE_NAME)),
        "runtime_active": int(systemctl_state("active", SERVICE_NAME)),
        "authorized_uid": authorized_uid() if authorized_uid() is not None else "",
        "leases": int(runtime.get("lease_count", 0) or 0),
        "runtime_owns_module": int(bool(runtime.get("owns_module", False))),
    }


def inspect_backend():
    for key, value in inspect_values().items():
        print(f"{key}={value}")


def bundled_source():
    candidates = [INSTALLED_SOURCE, Path(__file__).resolve().parent / "cpuid_fault_emulation"]
    for source in candidates:
        if (source / "Makefile").is_file() and (source / "dkms.conf").is_file():
            return source
    raise BackendError("The packaged kernel module source is missing")


def copy_source(destination, owner=None):
    source = bundled_source()
    temporary = destination.with_name(f".{destination.name}.copying")
    shutil.rmtree(temporary, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, temporary)
    if owner:
        for path in [temporary, *temporary.rglob("*")]:
            os.chown(path, owner.pw_uid, owner.pw_gid)
    shutil.rmtree(destination, ignore_errors=True)
    temporary.replace(destination)
    return destination


def validate_source(source):
    required = (
        source / "Makefile",
        source / "dkms.conf",
        source / "src/cpuid_fault_emulation.c",
        source / "src/capture_context.S",
        source / "src/run_vm.S",
    )
    if not all(path.is_file() for path in required):
        raise BackendError("Kernel module source is incomplete")
    print("Kernel module source is ready.", flush=True)


def install_dependencies():
    kernel = os.uname().release
    if shutil.which("pacman"):
        pkgbase = Path(f"/usr/lib/modules/{kernel}/pkgbase")
        package = pkgbase.read_text(encoding="utf-8").strip() if pkgbase.is_file() else "linux"
        run(["pacman", "-S", "--needed", "--noconfirm", "dkms", "base-devel", f"{package}-headers"])
    elif shutil.which("apt-get"):
        run(["apt-get", "update"])
        run(["apt-get", "install", "-y", "dkms", "build-essential", f"linux-headers-{kernel}"])
    elif shutil.which("dnf"):
        run(["dnf", "install", "-y", "dkms", "gcc", "make", "binutils", f"kernel-devel-{kernel}"])
    elif shutil.which("yum"):
        run(["yum", "install", "-y", "dkms", "gcc", "make", "binutils", f"kernel-devel-{kernel}"])
    elif shutil.which("zypper"):
        run(["zypper", "--non-interactive", "install", "dkms", "gcc", "make", "binutils", "kernel-devel"])
    else:
        raise BackendError("No supported package manager found")


def install_dkms():
    if not shutil.which("dkms"):
        raise BackendError("DKMS is unavailable")
    status = quiet(["dkms", "status", "-m", "cpuid_fault_emulation", "-v", "0.1"])
    registered = "cpuid_fault_emulation/0.1" in status.stdout
    backup = STATE_DIR / "dkms-source-backup"
    shutil.rmtree(backup, ignore_errors=True)
    old_source = Path("/var/lib/dkms/cpuid_fault_emulation/0.1/source")
    if registered and old_source.exists():
        shutil.copytree(old_source.resolve(), backup)

    source = copy_source(SOURCE_DIR)
    validate_source(source)
    if registered:
        run(["dkms", "remove", "cpuid_fault_emulation/0.1", "--all"])
    try:
        run(["dkms", "add", str(source)])
        run(["dkms", "build", "cpuid_fault_emulation/0.1", "--force"])
        run(["dkms", "install", "cpuid_fault_emulation/0.1", "--force"])
    except Exception:
        quiet(["dkms", "remove", "cpuid_fault_emulation/0.1", "--all"])
        if backup.is_dir():
            run(["dkms", "add", str(backup)])
            run(["dkms", "build", "cpuid_fault_emulation/0.1", "--force"])
            run(["dkms", "install", "cpuid_fault_emulation/0.1", "--force"])
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)
    print("Module installed successfully.")


def build_container():
    system = gaming_os()
    if system not in {"bazzite", "steamos"}:
        raise BackendError("Container builds are only supported on Bazzite and SteamOS")
    account = desktop_account()
    source = copy_source(Path(account.pw_dir) / ".cache/uwu-launcher/module-source", account)
    validate_source(source)
    for tool in ("git", "podman", "runuser"):
        if not shutil.which(tool):
            raise BackendError(f"Required command not found: {tool}")
    if subprocess.run(as_desktop_user(["podman", "--version"]), stdout=subprocess.DEVNULL).returncode:
        raise BackendError("Podman is unavailable to the desktop user")

    repo = "bazzite-build-container" if system == "bazzite" else "deck-build-container"
    image = repo
    base = Path(account.pw_dir) / ".cache/uwu-launcher/build-containers"
    checkout = base / repo
    run(["mkdir", "-p", str(base)], user=True)
    if (checkout / ".git").is_dir():
        run(["git", "-C", str(checkout), "pull", "--ff-only"], user=True)
    elif checkout.exists():
        raise BackendError(f"Build path is not a Git checkout: {checkout}")
    else:
        run(["git", "clone", "--depth", "1", f"https://github.com/PareidoliaDev/{repo}.git", str(checkout)], user=True)

    environment = os.environ.copy()
    environment.update(IMAGE_NAME=image, CONTAINER_RUNTIME="podman")
    run(["bash", "./build.sh", "--pull"], cwd=checkout, user=True, env=environment)
    podman = ["podman", "run", "--rm", "--security-opt", "label=disable"]
    if system == "steamos":
        podman += ["-v", "/etc:/host/etc:ro"]
    podman += [
        "-v", f"{source}:/work", image, "bash", "-lc",
        'build_link="/lib/modules/$KERNEL_RELEASE/build"; '
        'if [ ! -e "$build_link" ]; then mkdir -p "$(dirname "$build_link")"; '
        'ln -sfn "$KERNEL_HEADERS" "$build_link"; fi; make clean && make',
    ]
    run(podman, user=True)
    built = source / "cpuid_fault_emulation.ko"
    if not built.is_file():
        raise BackendError(f"Build did not produce {built}")
    vermagic = quiet(["modinfo", "-F", "vermagic", str(built)])
    if vermagic.returncode or vermagic.stdout.split(" ", 1)[0].strip() != os.uname().release:
        raise BackendError("The built module does not match the running kernel")
    STATE_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = STATE_DIR / ".cpuid_fault_emulation.ko.tmp"
    shutil.copyfile(built, temporary)
    temporary.chmod(0o644)
    temporary.replace(MODULE_FILE)
    print(f"Module compiled and installed for {os.uname().release}.")


@contextmanager
def module_lock():
    MODULE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with MODULE_LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def restore_kvm(modules):
    for name in reversed(modules):
        if not kernel_module_loaded(name):
            run(["modprobe", name])


def _start_backend():
    if not module_installed():
        raise BackendError("The CPUID fault emulation module is not installed")
    if module_loaded():
        print("The module is already running.")
        return False
    if KVM_STATE.is_file():
        restore_kvm(KVM_STATE.read_text(encoding="utf-8").split())
        KVM_STATE.unlink()
    if not module_matches():
        raise BackendError(f"The module does not match kernel {os.uname().release}")

    removed = []
    try:
        for name in ("kvm_amd", "kvm"):
            if kernel_module_loaded(name):
                run(["modprobe", "-r", name])
                removed.append(name)
        KVM_STATE.write_text("\n".join(removed) + "\n", encoding="utf-8")
        if local_module() and MODULE_FILE.is_file():
            run(["insmod", str(MODULE_FILE)])
        else:
            run(["modprobe", "cpuid_fault_emulation"])
        if not module_loaded():
            raise BackendError("The module failed to start")
    except Exception:
        if module_loaded():
            quiet(["rmmod", "cpuid_fault_emulation"])
        restore_kvm(removed)
        KVM_STATE.unlink(missing_ok=True)
        raise
    print("Module started successfully.")
    return True


def _stop_backend():
    was_loaded = module_loaded()
    if was_loaded:
        if local_module() and MODULE_FILE.is_file():
            run(["rmmod", "cpuid_fault_emulation"])
        else:
            run(["modprobe", "-r", "cpuid_fault_emulation"])
    if KVM_STATE.is_file():
        modules = KVM_STATE.read_text(encoding="utf-8").split()
    elif was_loaded:
        modules = ["kvm_amd", "kvm"]
    else:
        print("The module is already stopped.")
        return
    restore_kvm(modules)
    KVM_STATE.unlink(missing_ok=True)
    if module_loaded():
        raise BackendError("The module failed to stop")
    print("Module stopped successfully.")


def start_backend():
    with module_lock():
        return _start_backend()


def stop_backend():
    snapshot = runtime_snapshot()
    if int(snapshot.get("lease_count", 0) or 0) > 0:
        raise BackendError("Close CPUID-enabled UwU games before stopping the module")
    with module_lock():
        _stop_backend()


def load_metadata():
    try:
        data = json.loads(METADATA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_metadata(data):
    STATE_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = METADATA_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(METADATA_FILE)


def configure_authorized_user():
    account = desktop_account()
    if account.pw_uid == 0:
        raise BackendError("A non-root desktop user is required")
    STATE_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = AUTHORIZED_UID_FILE.with_suffix(".tmp")
    temporary.write_text(f"{account.pw_uid}\n", encoding="ascii")
    temporary.chmod(0o644)
    temporary.replace(AUTHORIZED_UID_FILE)
    return account


def legacy_service_state():
    return {
        "enabled": systemctl_state("enabled", LEGACY_SERVICE_NAME),
        "active": systemctl_state("active", LEGACY_SERVICE_NAME),
    }


def disable_legacy_watcher():
    state = legacy_service_state()
    if state["enabled"] or state["active"]:
        metadata = load_metadata()
        if "legacy_watcher" not in metadata:
            metadata["legacy_watcher"] = state
            save_metadata(metadata)
        print("Disabling the legacy Steam-log HV watcher; UwU uses hosted-game leases instead.")
        run(["systemctl", "disable", "--now", LEGACY_SERVICE_NAME])


def restore_legacy_watcher():
    metadata = load_metadata()
    previous = metadata.pop("legacy_watcher", None)
    if previous:
        if previous.get("enabled"):
            run(["systemctl", "enable", LEGACY_SERVICE_NAME])
        if previous.get("active"):
            run(["systemctl", "start", LEGACY_SERVICE_NAME])
        save_metadata(metadata)
        print("Restored the previously configured HV Steam watcher.")


def enable_runtime_backend():
    if not shutil.which("systemctl"):
        raise BackendError("systemd is required for automatic hosted-game activation")
    configure_authorized_user()
    disable_legacy_watcher()
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", SERVICE_NAME])
    print("Automatic activation is ready for CPUID-enabled UwU games.")


def disable_runtime_backend(restore_legacy=True):
    if shutil.which("systemctl"):
        run(["systemctl", "disable", "--now", SERVICE_NAME])
    if restore_legacy:
        restore_legacy_watcher()
    print("Automatic UwU hosted-game activation is disabled.")


def install_backend():
    disable_legacy_watcher()
    if shutil.which("systemctl"):
        quiet(["systemctl", "stop", SERVICE_NAME])
    if local_module():
        build_container()
    else:
        install_dependencies()
        install_dkms()
    enable_runtime_backend()


def update_backend():
    disable_legacy_watcher()
    if shutil.which("systemctl"):
        quiet(["systemctl", "stop", SERVICE_NAME])
    if module_loaded():
        with module_lock():
            _stop_backend()
    if local_module():
        build_container()
    else:
        install_dependencies()
        install_dkms()
    enable_runtime_backend()


def uninstall_backend():
    disable_runtime_backend(restore_legacy=False)
    with module_lock():
        if module_loaded() or KVM_STATE.is_file():
            _stop_backend()
        if local_module():
            MODULE_FILE.unlink(missing_ok=True)
            print(f"Removed {MODULE_FILE}.")
        else:
            status = quiet(["dkms", "status", "-m", "cpuid_fault_emulation", "-v", "0.1"])
            if "cpuid_fault_emulation/0.1" in status.stdout:
                run(["dkms", "remove", "cpuid_fault_emulation/0.1", "--all"])
                if shutil.which("depmod"):
                    run(["depmod"])
            else:
                print("No DKMS module registration was found.")
    print("CPUID Fault Emulation was removed.")


def bootloader():
    if Path("/etc/default/limine").is_file():
        return "limine"
    if Path("/etc/default/grub").is_file():
        return "grub"
    if Path("/boot/loader/entries").is_dir():
        return "systemd-boot"
    if shutil.which("bootctl") and quiet(["bootctl", "is-installed"]).returncode == 0:
        return "systemd-boot"
    raise BackendError("No supported bootloader found")


def replace_kernel_arg(text, kind, present):
    token = "clearcpuid=514"
    if kind == "grub":
        pattern = re.compile(r'^(GRUB_CMDLINE_LINUX_DEFAULT=)(?:"([^"]*)"|(.*))$', re.M)
        match = pattern.search(text)
        args = (match.group(2) if match and match.group(2) is not None else match.group(3) if match else "") or ""
        values = [value for value in args.split() if value != token]
        if present:
            values.append(token)
        line = f'GRUB_CMDLINE_LINUX_DEFAULT="{" ".join(values)}"'
        return pattern.sub(line, text, count=1) if match else text.rstrip() + "\n" + line + "\n"
    lines = text.splitlines()
    if kind == "limine":
        lines = [line for line in lines if line.strip() != "KERNEL_CMDLINE[default]+=clearcpuid=514"]
        if present:
            lines.append("KERNEL_CMDLINE[default]+=clearcpuid=514")
    else:
        changed = False
        for index, line in enumerate(lines):
            if re.match(r"^\s*options(?:\s|$)", line):
                values = [value for value in line.split() if value != token]
                if present:
                    values.append(token)
                lines[index] = " ".join(values)
                changed = True
                break
        if not changed:
            raise BackendError("No options line was found in the boot entry")
    return "\n".join(lines) + "\n"


def atomic_write(path, text):
    metadata = path.stat()
    temporary = path.with_name(f".{path.name}.uwu.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(metadata.st_mode & 0o777)
    os.chown(temporary, metadata.st_uid, metadata.st_gid)
    temporary.replace(path)


def update_grub():
    if shutil.which("update-grub"):
        run(["update-grub"])
    elif shutil.which("grub-mkconfig"):
        run(["grub-mkconfig", "-o", "/boot/grub/grub.cfg"])
    elif shutil.which("grub2-mkconfig"):
        output = "/boot/grub2/grub.cfg" if Path("/boot/grub2").is_dir() else "/boot/grub/grub.cfg"
        run(["grub2-mkconfig", "-o", output])
    else:
        raise BackendError("No GRUB configuration generator was found")


def valid_boot_entry(value):
    entry = Path(value).resolve()
    parent = Path("/boot/loader/entries").resolve()
    if entry.parent != parent or entry.suffix != ".conf" or not entry.is_file():
        raise BackendError(f"Invalid boot entry: {value}")
    return entry


def change_umip(present, entry_value=None):
    token = "clearcpuid=514"
    if gaming_os() == "bazzite":
        if not shutil.which("rpm-ostree"):
            raise BackendError("rpm-ostree is unavailable")
        configured = clearcpuid_configured()
        if configured != present:
            run(["rpm-ostree", "kargs", "--append=" + token if present else "--delete=" + token])
    else:
        kind = bootloader()
        if kind == "systemd-boot":
            path = valid_boot_entry(entry_value or "")
        else:
            path = Path("/etc/default/limine" if kind == "limine" else "/etc/default/grub")
        original = path.read_text(encoding="utf-8")
        changed = replace_kernel_arg(original, kind, present)
        if changed != original:
            atomic_write(path, changed)
            try:
                if kind == "limine":
                    run(["limine-update"])
                elif kind == "grub":
                    update_grub()
            except Exception:
                atomic_write(path, original)
                try:
                    if kind == "limine":
                        run(["limine-update"])
                    elif kind == "grub":
                        update_grub()
                except Exception:
                    pass
                raise
    print(("Applied " if present else "Removed ") + token + ". Please restart.")


def process_identity(pid):
    try:
        path = Path(f"/proc/{int(pid)}")
        owner = path.stat().st_uid
        fields = (path / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return owner, fields[19]
    except (OSError, ValueError, IndexError):
        return None


def reap_leases(leases, identity=process_identity):
    """Drop leases whose owner process exited or whose PID was reused."""
    return {
        token: lease
        for token, lease in leases.items()
        if identity(lease.get("pid")) == (lease.get("uid"), str(lease.get("starttime")))
    }


def validate_runtime_request(request, peer_pid, peer_uid, allowed_uid):
    if peer_uid != allowed_uid:
        raise BackendError("This user is not authorized for UwU CPUID activation")
    if not isinstance(request, dict):
        raise BackendError("Invalid runtime request")
    action = request.get("action")
    if action not in {"acquire", "release", "status"}:
        raise BackendError("Unsupported runtime action")
    if action == "acquire":
        if request.get("pid") != peer_pid:
            raise BackendError("A runtime lease may only track the requesting process")
        identity = process_identity(peer_pid)
        if identity != (peer_uid, str(request.get("starttime", ""))):
            raise BackendError("The requesting process identity could not be verified")
        game_id = request.get("game_id", "")
        if not isinstance(game_id, str) or len(game_id) > 200:
            raise BackendError("Invalid game identifier")
    if action == "release":
        token = request.get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise BackendError("Invalid runtime lease token")
    return action


class RuntimeServer:
    def __init__(self):
        self.allowed_uid = authorized_uid()
        if self.allowed_uid is None:
            raise BackendError("Automatic activation has not been configured for a desktop user")
        self.leases = {}
        self.owns_module = RUNTIME_OWNS_MODULE.exists() and module_loaded()
        self.stopping = False
        self.listener = None
        self._load_leases()

    def _load_leases(self):
        state = runtime_snapshot()
        leases = state.get("leases", {})
        if isinstance(leases, dict):
            self.leases = reap_leases(leases)

    def _write_state(self):
        RUNTIME_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
        state = {
            "lease_count": len(self.leases),
            "owns_module": self.owns_module,
            "leases": self.leases,
        }
        fd, name = tempfile.mkstemp(prefix="runtime-state.", dir=RUNTIME_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, indent=2)
                stream.write("\n")
            os.chmod(name, 0o644)
            os.replace(name, RUNTIME_STATE)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def reconcile(self):
        self.leases = reap_leases(self.leases)
        if self.leases and not module_loaded():
            with module_lock():
                started = _start_backend()
            if started:
                self.owns_module = True
                RUNTIME_OWNS_MODULE.touch(mode=0o600, exist_ok=True)
        elif not self.leases and self.owns_module:
            with module_lock():
                _stop_backend()
            self.owns_module = False
            RUNTIME_OWNS_MODULE.unlink(missing_ok=True)
        self._write_state()

    def acquire(self, request, peer_pid, peer_uid):
        token = secrets.token_hex(16)
        self.leases[token] = {
            "pid": peer_pid,
            "uid": peer_uid,
            "starttime": str(request["starttime"]),
            "game_id": request.get("game_id", ""),
        }
        try:
            self.reconcile()
        except Exception:
            self.leases.pop(token, None)
            self._write_state()
            raise
        return {"ok": True, "token": token, "loaded": module_loaded()}

    def release(self, request, peer_uid):
        token = request["token"]
        lease = self.leases.get(token)
        if lease and lease.get("uid") != peer_uid:
            raise BackendError("The lease belongs to another user")
        self.leases.pop(token, None)
        self.reconcile()
        return {"ok": True, "loaded": module_loaded()}

    def handle(self, connection):
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        peer_pid, peer_uid, _peer_gid = struct.unpack("3i", credentials)
        connection.settimeout(10)
        raw = b""
        while b"\n" not in raw and len(raw) <= 65536:
            chunk = connection.recv(4096)
            if not chunk:
                break
            raw += chunk
        try:
            request = json.loads(raw.split(b"\n", 1)[0].decode("utf-8"))
            action = validate_runtime_request(request, peer_pid, peer_uid, self.allowed_uid)
            if action == "acquire":
                response = self.acquire(request, peer_pid, peer_uid)
            elif action == "release":
                response = self.release(request, peer_uid)
            else:
                self.reconcile()
                response = {
                    "ok": True,
                    "loaded": module_loaded(),
                    "lease_count": len(self.leases),
                    "owns_module": self.owns_module,
                }
        except Exception as error:
            response = {"ok": False, "error": str(error)}
        connection.sendall(json.dumps(response).encode("utf-8") + b"\n")

    def run(self):
        RUNTIME_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
        RUNTIME_SOCKET.unlink(missing_ok=True)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(RUNTIME_SOCKET))
        os.chown(RUNTIME_SOCKET, self.allowed_uid, -1)
        os.chmod(RUNTIME_SOCKET, 0o600)
        self.listener.listen(8)
        self.listener.settimeout(0.5)

        def stop(*_args):
            self.stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        self.reconcile()
        try:
            while not self.stopping:
                try:
                    connection, _ = self.listener.accept()
                except socket.timeout:
                    self.reconcile()
                    continue
                with connection:
                    self.handle(connection)
        finally:
            RUNTIME_SOCKET.unlink(missing_ok=True)
            self.leases = reap_leases(self.leases)
            if self.owns_module:
                try:
                    with module_lock():
                        _stop_backend()
                finally:
                    self.owns_module = False
                    RUNTIME_OWNS_MODULE.unlink(missing_ok=True)
            self._write_state()
            self.listener.close()


def cpuid_probe():
    if os.uname().machine.lower() not in {"x86_64", "amd64"}:
        return 4
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    code = bytes.fromhex("534989d089f889f10fa241890041895804418948084189500c5bc3")
    memory = mmap.mmap(-1, len(code), prot=mmap.PROT_READ | mmap.PROT_WRITE)
    memory.write(code)
    address = ctypes.addressof(ctypes.c_char.from_buffer(memory))
    page = address & ~(mmap.PAGESIZE - 1)
    if libc.mprotect(ctypes.c_void_p(page), ctypes.c_size_t(mmap.PAGESIZE), mmap.PROT_READ | mmap.PROT_EXEC):
        return 7
    cpuid_fn = ctypes.CFUNCTYPE(None, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32))(address)

    def cpuid(leaf):
        registers = (ctypes.c_uint32 * 4)()
        cpuid_fn(leaf, 0, registers)
        return registers

    native = cpuid(0x336933)

    def output(message):
        os.write(1, message.encode())

    output("Native leaf 0x336933: " + ", ".join(
        f"{name}=0x{value:08x}" for name, value in zip(("EAX", "EBX", "ECX", "EDX"), native)
    ) + "\n")

    class SigSet(ctypes.Structure):
        _fields_ = [("values", ctypes.c_ulong * 16)]

    handler_type = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

    class SigAction(ctypes.Structure):
        _fields_ = [("handler", handler_type), ("mask", SigSet), ("flags", ctypes.c_int), ("restorer", ctypes.c_void_p)]

    @handler_type
    def handler(_number, _info, context):
        registers = (ctypes.c_longlong * 23).from_address(context + 40)
        if registers[13] != 0x336933 or ctypes.string_at(registers[16], 2) != b"\x0f\xa2":
            os._exit(5)
        registers[13], registers[11], registers[14], registers[12] = 0x1337, 0, 0, 0
        registers[16] += 2

    action = SigAction()
    action.handler = handler
    action.flags = 4
    if libc.sigemptyset(ctypes.byref(action.mask)) or libc.sigaction(signal.SIGSEGV, ctypes.byref(action), None):
        return 6
    if libc.syscall(158, 0x1012, 0) == -1:
        output("ARCH_SET_CPUID failed; CPUID faulting is unavailable.\n")
        return 2
    output("Running isolated CPUID fault test.\n")
    result = cpuid(0x336933)
    libc.syscall(158, 0x1012, 1)
    output(f"Spoofed EAX=0x{result[0]:08x}.\n" + ("Bypass works.\n" if result[0] == 0x1337 else "Bypass failed.\n"))
    return 0 if result[0] == 0x1337 else 3


def verify_privileged_backend():
    path = Path(__file__).resolve()
    metadata = path.stat()
    if os.geteuid() != 0:
        raise BackendError("Administrator privileges are required")
    if not path.is_relative_to(INSTALLED_ROOT) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise BackendError("Privileged actions require the root-owned packaged helper")


def backend(action, values):
    if action not in ACTIONS:
        raise BackendError("Unknown backend action")
    if ACTIONS[action]:
        verify_privileged_backend()
    dispatch = {
        "inspect": inspect_backend,
        "install": install_backend,
        "update": update_backend,
        "uninstall": uninstall_backend,
        "start": start_backend,
        "stop": stop_backend,
        "enable_runtime": enable_runtime_backend,
        "disable_runtime": disable_runtime_backend,
        "bootloader": lambda: print(bootloader()),
        "disable_umip": lambda: change_umip(True),
        "enable_umip": lambda: change_umip(False),
        "disable_umip_entry": lambda: change_umip(True, values[0] if values else None),
        "enable_umip_entry": lambda: change_umip(False, values[0] if values else None),
        "reboot": lambda: run(["systemctl", "reboot"]),
    }
    dispatch[action]()


def main():
    if "--cpuid-probe" in sys.argv:
        raise SystemExit(cpuid_probe())
    if len(sys.argv) >= 3 and sys.argv[1] == "--backend":
        backend(sys.argv[2], sys.argv[3:])
        return
    if len(sys.argv) == 2 and sys.argv[1] == "--runtime-daemon":
        verify_privileged_backend()
        RuntimeServer().run()
        return
    raise BackendError("No valid helper mode was selected")


if __name__ == "__main__":
    try:
        main()
    except BackendError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
