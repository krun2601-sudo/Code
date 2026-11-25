"""Install the reviewed policy package into a ZR10 platform checkout.

The command is a dry-run unless ``--apply`` is supplied.  It touches only
``algorithms/__init__.py`` and ``algorithms/user_policy.py`` under the exact
platform root selected by the operator.  Existing different files are never
overwritten unless ``--replace-existing`` is also explicit; every replacement
is backed up and described by a machine-readable report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


FILES = ("__init__.py", "user_policy.py")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.",
                                               suffix=".tmp", dir=destination.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def install(platform_root: Path, *, apply: bool, replace_existing: bool) -> dict:
    root = platform_root.resolve()
    if not (root / "zr10lab" / "config.py").is_file():
        raise ValueError(f"not a ZR10 platform root: {root}")
    source_root = Path(__file__).resolve().parent
    target_root = root / "algorithms"
    rows = []
    for name in FILES:
        source = source_root / name
        target = target_root / name
        if not source.is_file():
            raise ValueError(f"missing policy source: {source}")
        source_hash = sha256(source)
        target_hash = sha256(target) if target.is_file() else None
        if target_hash == source_hash:
            action = "unchanged"
        elif target_hash is None:
            action = "create"
        elif replace_existing:
            action = "replace"
        else:
            raise ValueError(
                f"refusing to overwrite different file {target}; review it and rerun with --replace-existing"
            )
        rows.append({
            "relative_path": f"algorithms/{name}",
            "source_sha256": source_hash,
            "previous_sha256": target_hash,
            "action": action,
        })

    result = {
        "operation": "install_policy",
        "platform_root": str(root),
        "dry_run": not apply,
        "files": rows,
        "report_path": None,
    }
    if not apply:
        return result

    install_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    backup_root = root / ".ral_hil_backups" / f"policy_{install_id}"
    backup_root.mkdir(parents=True, exist_ok=False)
    for row in rows:
        relative = Path(row["relative_path"])
        target = root / relative
        if row["action"] == "replace":
            backup = backup_root / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
            row["backup_path"] = str(backup)
        else:
            row["backup_path"] = None
        if row["action"] != "unchanged":
            atomic_copy(source_root / relative.name, target)
        row["installed_sha256"] = sha256(target)
        if row["installed_sha256"] != row["source_sha256"]:
            raise RuntimeError(f"post-install hash mismatch: {target}")

    report_path = backup_root / "install_report.json"
    result["dry_run"] = False
    result["report_path"] = str(report_path)
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform-root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--replace-existing", action="store_true")
    args = parser.parse_args()
    try:
        result = install(args.platform_root, apply=args.apply,
                         replace_existing=args.replace_existing)
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
