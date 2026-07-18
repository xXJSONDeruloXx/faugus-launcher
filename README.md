# UwU Launcher

UwU Launcher is a side-by-side build of [Faugus Launcher 2.0](https://github.com/Faugus/faugus-launcher) for games that need CPUID Fault Emulation. It uses [UMU-Launcher](https://github.com/Open-Wine-Components/umu-launcher) and integrates the kernel-module setup from [HV Installer GTK](https://github.com/xXJSONDeruloXx/hv-installer-gtk).

## What is different

- Installs as `uwu-launcher` without replacing Faugus.
- Uses independent config, data, state, runner, icon, shortcut, and prefix paths.
- Adds **Use CPUID Compatibility** to every hosted game, enabled by default.
- Acquires the module before the game process starts.
- Keeps it active while any opted-in hosted game remains open.
- Releases it after the final game exits, including launcher crashes via PID lease cleanup.
- Provides installation, update, diagnostics, manual start/stop, UMIP, and automatic-runtime controls under **Settings → CPUID Compatibility Manager**.
- Does not use Steam-log or non-Steam-shortcut watching. Steam-owned games are excluded from the hosted-game option.
- Pulls checksum-pinned `cachyos_11.0_20260702-LinUwUx` and `GE-Proton11-1-LinUwUx` release assets on demand; the CachyOS build is the actual default.

## LinUwUx Proton runners

The large runner archives are published as checksum-pinned assets on the `uwu-runners-20260702` GitHub release. UwU downloads and safely extracts a runner the first time it is needed:

```text
~/.local/share/uwu-launcher/runners/cachyos_11.0_20260702-LinUwUx/
~/.local/share/uwu-launcher/runners/GE-Proton11-1-LinUwUx/
```

Every download is verified against the packaged size and SHA-256 manifest before extraction. CachyOS remains the actual default, while both runners stay isolated from Faugus and resolve to absolute `PROTONPATH` values after installation.

## Runtime design

The package installs a root-owned helper and `uwu-hv-runtime.service`. Setup authorizes one desktop UID. Its Unix socket is owned and readable/writable only by that user, and every acquire request is checked against the connecting PID, UID, and process start time.

The service uses reference-counted process leases, starts CPUID Fault Emulation before acknowledging a launch, and stops the module only when it started the module itself. KVM state and locking are shared with HV Installer GTK to avoid concurrent global module changes.

## Build from source

```sh
meson setup builddir --prefix=/usr
meson compile -C builddir
DESTDIR="$PWD/stage" meson install -C builddir
```

Run validation with:

```sh
python3 -m py_compile hv/hv_helper.py faugus/*.py
python3 -m unittest discover -s tests -v
desktop-file-validate builddir/data/*.desktop
appstreamcli validate --no-net data/io.github.xXJSONDeruloXx.uwu-launcher.metainfo.xml
```

## Independent paths

```text
Executable:       /usr/bin/uwu-launcher
Private code:     /usr/lib/uwu-launcher/
Configuration:    ~/.config/uwu-launcher/
Application data: ~/.local/share/uwu-launcher/
State:            ~/.local/state/uwu-launcher/
Prefixes:         ~/UwU/
Release runners:  ~/.local/share/uwu-launcher/runners/
```

The upstream Faugus installation and `~/Faugus/` are not modified.
