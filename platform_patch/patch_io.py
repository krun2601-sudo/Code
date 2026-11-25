"""Transactional installer helpers for the exact ZR10 platform snapshot.

No configuration file is read.  Reports contain file hashes and operation
status only, so device addresses and RTSP URLs can never enter patch records.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .patchset import PATCH_VERSION, PatchError, transform_bytes
except ImportError:  # Direct script execution from this directory.
    from patchset import PATCH_VERSION, PatchError, transform_bytes


HERE = Path(__file__).resolve().parent
MANIFEST_PATH = HERE / "manifest.json"
BACKUP_DIRNAME = ".ral_hil_patch_backups"
REPORT_NAME = "install_report.json"


class InstallError(RuntimeError):
    """A safety check or transactional operation failed."""


@dataclass(frozen=True)
class FileState:
    relative_path: str
    sha256: str | None
    state: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("patch_version") != PATCH_VERSION:
        raise InstallError("manifest patch_version does not match patchset.py")
    if not isinstance(manifest.get("targets"), dict) or not manifest["targets"]:
        raise InstallError("manifest has no patch targets")
    return manifest


def resolve_platform_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise InstallError(f"platform root is not a directory: {root}")
    if not (root / "zr10lab").is_dir():
        raise InstallError(f"platform root has no zr10lab package: {root}")
    return root


def inspect_platform(root: Path, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = manifest or load_manifest()
    version_path = root / manifest["platform_version_file"]
    version_hash = sha256_file(version_path) if version_path.is_file() else None
    version_state = "matched" if version_hash == manifest["platform_version_sha256"] else "mismatch"
    files: list[FileState] = []
    for relative, expected in manifest["targets"].items():
        path = root / relative
        actual = sha256_file(path) if path.is_file() else None
        if actual == expected["original_sha256"]:
            state = "original"
        elif actual == expected["patched_sha256"]:
            state = "patched"
        else:
            state = "missing" if actual is None else "drift"
        files.append(FileState(relative, actual, state))
    states = {item.state for item in files}
    if version_state != "matched" or states & {"missing", "drift"}:
        overall = "drift"
    elif states == {"original"}:
        overall = "original"
    elif states == {"patched"}:
        overall = "patched"
    else:
        overall = "mixed"
    return {
        "platform_version": manifest["platform_version"],
        "patch_version": manifest["patch_version"],
        "state": overall,
        "version_file_state": version_state,
        "version_file_sha256": version_hash,
        "files": [item.__dict__ for item in files],
    }


def require_state(root: Path, expected: str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    status = inspect_platform(root, manifest)
    if status["state"] != expected:
        details = ", ".join(f"{row['relative_path']}={row['state']}" for row in status["files"])
        raise InstallError(
            f"platform state is {status['state']}, expected {expected}; {details}"
        )
    return status


def build_patched_files(root: Path, manifest: dict[str, Any] | None = None) -> dict[str, bytes]:
    manifest = manifest or load_manifest()
    require_state(root, "original", manifest)
    patched: dict[str, bytes] = {}
    for relative, expected in manifest["targets"].items():
        source = (root / relative).read_bytes()
        try:
            result = transform_bytes(relative, source)
        except PatchError as exc:
            raise InstallError(str(exc)) from exc
        actual = sha256_bytes(result)
        if actual != expected["patched_sha256"]:
            raise InstallError(
                f"internal patch hash mismatch for {relative}: {actual}"
            )
        patched[relative] = result
    return patched


def atomic_write(path: Path, data: bytes) -> None:
    """Durably stage a sibling and replace one file without a partial write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write(path, data)


def prepare_overlay(root: Path, patched: dict[str, bytes], destination: Path) -> Path:
    overlay = destination / "platform_overlay"
    shutil.copytree(root / "zr10lab", overlay / "zr10lab")
    for relative, data in patched.items():
        atomic_write(overlay / relative, data)
    return overlay


def _python_env(overlay: Path) -> dict[str, str]:
    env = dict(os.environ)
    prior = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(overlay) + (os.pathsep + prior if prior else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def redact_sensitive(text: str) -> str:
    """Prevent local endpoints from escaping through failed test output."""
    text = re.sub(r"(?i)\brtsps?://[^\s'\"<>]+", "rtsp://<redacted>", text)
    return re.sub(
        r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\d.])",
        "<redacted-ip>", text,
    )


def run_preflight(
    root: Path,
    patched: dict[str, bytes],
    python: str | Path | None = None,
    full_tests: bool = False,
) -> dict[str, Any]:
    interpreter = str(Path(python).resolve()) if python else sys.executable
    if not Path(interpreter).is_file():
        raise InstallError(f"python interpreter does not exist: {interpreter}")
    # Keep the overlay beside the platform rather than in a system temp that
    # may live on another volume or be blocked by Windows application sandbox
    # ACLs.  The directory is removed before this function returns.
    with tempfile.TemporaryDirectory(prefix=".ral_hil_patch_preflight_", dir=root.parent) as name:
        temporary = Path(name)
        overlay = prepare_overlay(root, patched, temporary)
        commands = [
            [interpreter, "-B", "-m", "compileall", "-q", str(overlay / "zr10lab")],
            [interpreter, "-B", str(HERE / "smoke_test.py"), "--platform-root", str(overlay)],
        ]
        if full_tests:
            tests = root / "tests"
            if not tests.is_dir():
                raise InstallError("--full-tests requested but platform has no tests directory")
            bootstrap = (
                "import sys; from pathlib import Path; "
                "root=Path(sys.argv.pop(1)).resolve(); sys.path.insert(0,str(root)); "
                "import zr10lab; from zr10lab.models import FrameStatus; import pytest; "
                "assert Path(zr10lab.__file__).resolve().is_relative_to(root); "
                "raise SystemExit(pytest.main(sys.argv[1:]))"
            )
            commands.append([
                interpreter, "-B", "-c", bootstrap, str(overlay), "-q", str(tests),
                "--basetemp", str(temporary / "pytest_tmp"),
            ])
        outcomes = []
        for command in commands:
            result = subprocess.run(
                command,
                cwd=root,
                env=_python_env(overlay),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=300,
                check=False,
            )
            if len(command) > 3 and command[2] == "-m":
                check_name = command[3]
            elif len(command) > 3 and command[2] == "-c":
                check_name = "pytest"
            else:
                check_name = Path(command[2]).name if len(command) > 2 else "python"
            outcome: dict[str, Any] = {"name": check_name, "returncode": result.returncode}
            if check_name == "pytest" and result.returncode == 0:
                matches = re.findall(r"(?<!\d)(\d+) passed", result.stdout)
                if matches:
                    outcome["passed_tests"] = int(matches[-1])
            outcomes.append(outcome)
            if result.returncode:
                # Test output is returned to the operator, but never persisted in
                # the install report (it could contain local configuration data).
                output = redact_sensitive((result.stdout + "\n" + result.stderr).strip())
                raise InstallError(f"preflight command failed ({result.returncode}):\n{output}")
        return {"passed": True, "checks": outcomes, "full_tests": bool(full_tests)}


def create_backup(root: Path, manifest: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = root / BACKUP_DIRNAME / f"{stamp}_{uuid.uuid4().hex[:8]}"
    backup.mkdir(parents=True, exist_ok=False)
    files: dict[str, Any] = {}
    for relative, expected in manifest["targets"].items():
        source = root / relative
        target = backup / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        actual = sha256_file(target)
        if actual != expected["original_sha256"]:
            raise InstallError(f"backup verification failed for {relative}")
        files[relative] = {
            "original_sha256": expected["original_sha256"],
            "patched_sha256": expected["patched_sha256"],
        }
    report = {
        "report_schema": 1,
        "patch_version": manifest["patch_version"],
        "platform_version": manifest["platform_version"],
        "created_utc": utc_now(),
        "updated_utc": utc_now(),
        "status": "prepared",
        "files": files,
    }
    atomic_json(backup / REPORT_NAME, report)
    return backup, report


def install(
    root: Path,
    *,
    apply: bool = False,
    python: str | Path | None = None,
    full_tests: bool = False,
) -> dict[str, Any]:
    manifest = load_manifest()
    before = require_state(root, "original", manifest)
    patched = build_patched_files(root, manifest)
    preflight = run_preflight(root, patched, python, full_tests)
    result: dict[str, Any] = {
        "operation": "install",
        "applied": False,
        "patch_version": manifest["patch_version"],
        "platform_version": manifest["platform_version"],
        "state_before": before["state"],
        "preflight": preflight,
        "target_count": len(patched),
    }
    if not apply:
        result["state_after"] = inspect_platform(root, manifest)["state"]
        return result

    backup, report = create_backup(root, manifest)
    # Restore from the verified backup, never from a second unverified live read.
    original_bytes = {relative: (backup / relative).read_bytes() for relative in patched}
    replaced: list[str] = []
    try:
        # Close the TOCTOU window between backup and commit.
        require_state(root, "original", manifest)
        for relative, data in patched.items():
            atomic_write(root / relative, data)
            replaced.append(relative)
        after = require_state(root, "patched", manifest)
        # A patch without a durable rollback record is not a successful commit.
        report.update(status="installed", updated_utc=utc_now(), error="")
        atomic_json(backup / REPORT_NAME, report)
    except Exception as exc:
        restore_errors = []
        for relative in reversed(replaced):
            try:
                atomic_write(root / relative, original_bytes[relative])
            except Exception as restore_exc:  # pragma: no cover - catastrophic filesystem failure
                restore_errors.append(f"{relative}: {restore_exc}")
        if not restore_errors:
            try:
                require_state(root, "original", manifest)
            except Exception as verify_exc:  # pragma: no cover - concurrent/catastrophic drift
                restore_errors.append(f"post-restore verification: {verify_exc}")
        report.update(status="install_failed_rolled_back" if not restore_errors else "install_failed_restore_incomplete",
                      updated_utc=utc_now(), error=f"{type(exc).__name__}: {exc}", restore_errors=restore_errors)
        try:
            atomic_json(backup / REPORT_NAME, report)
        except Exception as report_exc:  # pragma: no cover - disk/permission failure
            restore_errors.append(f"install report: {report_exc}")
        if restore_errors:
            raise InstallError(f"install failed and rollback was incomplete: {restore_errors}") from exc
        raise InstallError(f"install failed; originals restored: {exc}") from exc

    result.update(applied=True, state_after=after["state"], backup_id=backup.name)
    return result


def list_backups(root: Path) -> list[Path]:
    base = root / BACKUP_DIRNAME
    if not base.is_dir():
        return []
    return sorted(
        (item for item in base.iterdir() if item.is_dir() and (item / REPORT_NAME).is_file()),
        key=lambda item: item.name,
        reverse=True,
    )


def resolve_backup(
    root: Path,
    backup_id: str | None = None,
    *,
    accepted_statuses: tuple[str, ...] = ("installed",),
) -> tuple[Path, dict[str, Any]]:
    base = (root / BACKUP_DIRNAME).resolve()
    if backup_id:
        candidate = (base / backup_id).resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise InstallError("backup id escapes the backup directory") from exc
        choices = [candidate]
    else:
        choices = list_backups(root)
    for candidate in choices:
        report_path = candidate / REPORT_NAME
        if not report_path.is_file():
            continue
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("status") in accepted_statuses and report.get("patch_version") == PATCH_VERSION:
            return candidate, report
    raise InstallError("no compatible verified backup is available for rollback")


def rollback(root: Path, *, apply: bool = False, backup_id: str | None = None) -> dict[str, Any]:
    manifest = load_manifest()
    before = inspect_platform(root, manifest)
    # mixed means every target is still one of the two manifest-approved byte
    # images.  It can arise only from an interrupted multi-file commit and is
    # therefore safe to recover.  Any true drift or missing file is refused.
    if before["state"] not in ("patched", "mixed"):
        raise InstallError(f"platform state is {before['state']}, expected patched or recoverable mixed")
    backup, report = resolve_backup(
        root, backup_id,
        accepted_statuses=(
            "installed", "prepared", "rollback_prepared",
            "install_failed_restore_incomplete", "rollback_failed_restore_incomplete",
        ),
    )
    originals: dict[str, bytes] = {}
    current: dict[str, bytes] = {}
    for relative, expected in manifest["targets"].items():
        source = backup / relative
        if not source.is_file() or sha256_file(source) != expected["original_sha256"]:
            raise InstallError(f"backup is missing or corrupt: {relative}")
        originals[relative] = source.read_bytes()
        current[relative] = (root / relative).read_bytes()
    result: dict[str, Any] = {
        "operation": "rollback",
        "applied": False,
        "patch_version": manifest["patch_version"],
        "platform_version": manifest["platform_version"],
        "state_before": before["state"],
        "backup_id": backup.name,
        "target_count": len(originals),
    }
    if not apply:
        result["state_after"] = inspect_platform(root, manifest)["state"]
        return result
    report.update(status="rollback_prepared", updated_utc=utc_now(), error="")
    atomic_json(backup / REPORT_NAME, report)
    replaced: list[str] = []
    try:
        current_state = inspect_platform(root, manifest)["state"]
        if current_state not in ("patched", "mixed"):
            raise InstallError(f"platform changed before rollback commit: {current_state}")
        for relative, data in originals.items():
            atomic_write(root / relative, data)
            replaced.append(relative)
        after = require_state(root, "original", manifest)
    except Exception as exc:
        restore_errors = []
        for relative in reversed(replaced):
            try:
                atomic_write(root / relative, current[relative])
            except Exception as restore_exc:  # pragma: no cover
                restore_errors.append(f"{relative}: {restore_exc}")
        if not restore_errors:
            try:
                restored_state = inspect_platform(root, manifest)["state"]
                if restored_state != before["state"]:
                    raise InstallError(
                        f"post-restore state is {restored_state}, expected {before['state']}"
                    )
            except Exception as verify_exc:  # pragma: no cover
                restore_errors.append(f"post-restore verification: {verify_exc}")
        report.update(
            status="installed" if not restore_errors else "rollback_failed_restore_incomplete",
            updated_utc=utc_now(), last_rollback_error=f"{type(exc).__name__}: {exc}",
            restore_errors=restore_errors,
        )
        try:
            atomic_json(backup / REPORT_NAME, report)
        except Exception as report_exc:  # pragma: no cover
            restore_errors.append(f"rollback report: {report_exc}")
        raise InstallError(
            f"rollback failed; patched files were {'not fully ' if restore_errors else ''}restored: {exc}"
        ) from exc
    report.update(status="rolled_back", updated_utc=utc_now(), error="", restore_errors=[])
    atomic_json(backup / REPORT_NAME, report)
    result.update(applied=True, state_after=after["state"])
    return result


def print_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))

