from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest


PATCH_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = Path(__file__).resolve().parents[3]
KIT = WORKSPACE / "ZR10_Algorithm_Kit"
sys.path.insert(0, str(PATCH_ROOT.parent))

from platform_patch.patch_io import (  # noqa: E402
    InstallError,
    atomic_write,
    build_patched_files,
    create_backup,
    inspect_platform,
    install,
    load_manifest,
    redact_sensitive,
    rollback,
    sha256_bytes,
)
from platform_patch.patchset import transform_bytes  # noqa: E402


def _platform_copy(tmp_path: Path) -> Path:
    root = tmp_path / "platform"
    shutil.copytree(KIT / "zr10lab", root / "zr10lab")
    return root


def test_manifest_and_transforms_match_exact_snapshot():
    manifest = load_manifest()
    assert manifest["platform_version"] == "4.0.0"
    for relative, expected in manifest["targets"].items():
        source = (KIT / relative).read_bytes()
        assert sha256_bytes(source) == expected["original_sha256"]
        assert sha256_bytes(transform_bytes(relative, source)) == expected["patched_sha256"]


def test_failure_output_redacts_local_endpoints():
    value = redact_sensitive("source=rtsp://user:secret@10.20.30.40:8554/live peer 192.0.2.9")
    assert "secret" not in value
    assert "10.20.30.40" not in value
    assert "192.0.2.9" not in value


def test_dry_run_install_and_transactional_rollback(tmp_path):
    root = _platform_copy(tmp_path)
    dry = install(root, python=sys.executable)
    assert not dry["applied"]
    assert dry["state_before"] == dry["state_after"] == "original"

    applied = install(root, apply=True, python=sys.executable)
    assert applied["applied"]
    assert inspect_platform(root)["state"] == "patched"
    backup = root / ".ral_hil_patch_backups" / applied["backup_id"]
    assert (backup / "install_report.json").is_file()

    dry_rollback = rollback(root, backup_id=applied["backup_id"])
    assert not dry_rollback["applied"]
    assert inspect_platform(root)["state"] == "patched"

    restored = rollback(root, apply=True, backup_id=applied["backup_id"])
    assert restored["state_after"] == "original"
    assert inspect_platform(root)["state"] == "original"
    assert backup.is_dir(), "rollback must retain the verified backup"


def test_hash_gate_refuses_drift(tmp_path):
    root = _platform_copy(tmp_path)
    target = root / "zr10lab" / "models.py"
    target.write_bytes(target.read_bytes() + b"\n# local change\n")
    assert inspect_platform(root)["state"] == "drift"
    with pytest.raises(InstallError, match="expected original"):
        install(root, python=sys.executable)


def test_second_install_is_refused(tmp_path):
    root = _platform_copy(tmp_path)
    install(root, apply=True, python=sys.executable)
    with pytest.raises(InstallError, match="expected original"):
        install(root, python=sys.executable)


def test_rollback_recovers_interrupted_mixed_commit(tmp_path):
    root = _platform_copy(tmp_path)
    manifest = load_manifest()
    patched = build_patched_files(root, manifest)
    backup, _ = create_backup(root, manifest)
    for relative in tuple(patched)[:2]:
        atomic_write(root / relative, patched[relative])
    assert inspect_platform(root)["state"] == "mixed"
    restored = rollback(root, apply=True, backup_id=backup.name)
    assert restored["state_before"] == "mixed"
    assert restored["state_after"] == "original"
