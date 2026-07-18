"""Download and verify the LinUwUx runners published as release assets."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

from faugus.path_manager import COMPATIBILITY_DIR, PathManager

MARKER_NAME = ".uwu-release-runner.json"


class RunnerDownloadError(RuntimeError):
    pass


def _manifest_path():
    override = os.environ.get("UWU_RUNNER_MANIFEST")
    source = Path(__file__).resolve().parents[1] / "runners" / "manifest.json"
    installed = Path(PathManager.system_data("uwu-launcher/runner-manifest.json"))
    for candidate in filter(None, (Path(override) if override else None, source, installed)):
        if candidate.is_file():
            return candidate
    raise RunnerDownloadError("The release-runner manifest is missing.")


def load_manifest(path=None):
    manifest_path = Path(path) if path else _manifest_path()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        runners = manifest["runners"]
        release = manifest["release"]
        if not runners or not release["base_url"]:
            raise ValueError("empty release metadata")
        roots = [runner["root"] for runner in runners]
        if len(roots) != len(set(roots)):
            raise ValueError("duplicate runner roots")
        for runner in runners:
            int(runner["size"])
            if len(runner["sha256"]) != 64:
                raise ValueError("invalid SHA-256")
            if Path(runner["filename"]).name != runner["filename"]:
                raise ValueError("invalid asset filename")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as error:
        raise RunnerDownloadError(f"Invalid release-runner manifest: {error}") from error
    return manifest


def runner_records(manifest=None):
    manifest = manifest or load_manifest()
    return {runner["root"]: runner for runner in manifest["runners"]}


def release_runner_name(value, manifest=None):
    if not value:
        return None
    records = runner_records(manifest)
    value = str(value)
    if value in records:
        return value
    name = Path(value).name
    return name if Path(value).is_absolute() and name in records else None


def _marker_matches(target, runner, manifest):
    proton = target / "proton"
    vdf = target / "compatibilitytool.vdf"
    marker = target / MARKER_NAME
    if not proton.is_file() or not os.access(proton, os.X_OK) or not vdf.is_file() or not marker.is_file():
        return False
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        state.get("sha256") == runner["sha256"]
        and state.get("size") == runner["size"]
        and state.get("tag") == manifest["release"]["tag"]
    )


def validate_member(member, expected_root):
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != expected_root:
        raise RunnerDownloadError(f"Unsafe or unexpected archive member: {member.name}")


def _download(url, destination, runner, progress):
    request = urllib.request.Request(url, headers={"User-Agent": "uwu-launcher/2.0"})
    digest = hashlib.sha256()
    downloaded = 0
    expected_size = int(runner["size"])
    try:
        with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
            while True:
                chunk = response.read(4 * 1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
                if progress:
                    progress(downloaded, expected_size)
    except Exception as error:
        raise RunnerDownloadError(f"Could not download {runner['filename']}: {error}") from error

    if downloaded != expected_size:
        raise RunnerDownloadError(
            f"Unexpected size for {runner['filename']}: {downloaded} bytes (expected {expected_size})."
        )
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != runner["sha256"]:
        raise RunnerDownloadError(f"Checksum verification failed for {runner['filename']}.")


def ensure_release_runner(name, progress=None, manifest=None, compatibility_dir=None, base_url=None):
    manifest = manifest or load_manifest()
    records = runner_records(manifest)
    if name not in records:
        raise RunnerDownloadError(f"Unknown release runner: {name}")

    runner = records[name]
    compatibility_dir = Path(compatibility_dir or COMPATIBILITY_DIR)
    target = compatibility_dir / name
    compatibility_dir.mkdir(parents=True, exist_ok=True)

    lock_path = compatibility_dir / ".release-runners.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if _marker_matches(target, runner, manifest):
            return target

        if target.exists() or target.is_symlink():
            marker = target / MARKER_NAME
            if not marker.is_file():
                raise RunnerDownloadError(
                    f"{target} already exists and is not managed by UwU; rename or remove it first."
                )
            shutil.rmtree(target)

        asset_base = base_url or os.environ.get("UWU_RUNNER_BASE_URL") or manifest["release"]["base_url"]
        url = f"{asset_base.rstrip('/')}/{urllib.parse.quote(runner['filename'])}"
        archive = compatibility_dir / f".{runner['filename']}.part"
        extracting = compatibility_dir / f".{name}.installing-{os.getpid()}"
        archive.unlink(missing_ok=True)
        shutil.rmtree(extracting, ignore_errors=True)

        try:
            print(f"Downloading release runner {name}…", flush=True)
            _download(url, archive, runner, progress)
            print(f"Extracting release runner {name}…", flush=True)
            extracting.mkdir()
            with tarfile.open(archive, "r:*") as bundle:
                members = bundle.getmembers()
                for member in members:
                    validate_member(member, name)
                bundle.extractall(extracting, members=members, filter="data")

            extracted = extracting / name
            proton = extracted / "proton"
            if not proton.is_file() or not (extracted / "compatibilitytool.vdf").is_file():
                raise RunnerDownloadError(f"Release runner {name} is incomplete.")
            proton.chmod(proton.stat().st_mode | 0o111)
            (extracted / MARKER_NAME).write_text(
                json.dumps({
                    "tag": manifest["release"]["tag"],
                    "filename": runner["filename"],
                    "size": runner["size"],
                    "sha256": runner["sha256"],
                    "url": url,
                }, indent=2) + "\n",
                encoding="utf-8",
            )
            extracted.replace(target)
            print(f"Installed release runner {name} at {target}", flush=True)
            return target
        except RunnerDownloadError:
            raise
        except Exception as error:
            raise RunnerDownloadError(f"Could not install release runner {name}: {error}") from error
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(extracting, ignore_errors=True)


def main():
    manifest = load_manifest()
    choices = list(runner_records(manifest))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runner", choices=choices)
    args = parser.parse_args()

    last_percent = [-1]

    def show_progress(downloaded, total):
        percent = int(downloaded * 100 / total) if total else 0
        if percent // 10 != last_percent[0] // 10:
            print(f"{percent}%", flush=True)
            last_percent[0] = percent

    ensure_release_runner(args.runner, progress=show_progress, manifest=manifest)


if __name__ == "__main__":
    main()
