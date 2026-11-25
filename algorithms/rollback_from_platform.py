"""Rollback one policy installation report without overwriting later edits."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path


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


def rollback(report_path: Path, *, apply: bool) -> dict:
    report = json.loads(report_path.resolve().read_text(encoding="utf-8"))
    root = Path(report["platform_root"]).resolve()
    rows = report.get("files", [])
    actions = []
    for row in rows:
        target = root / row["relative_path"]
        if not target.is_file():
            raise ValueError(f"installed file is missing: {target}")
        if sha256(target) != row["installed_sha256"]:
            raise ValueError(f"refusing rollback because installed file changed: {target}")
        if row.get("previous_sha256") is None:
            action = "delete_created_file"
        elif row.get("backup_path"):
            backup = Path(row["backup_path"])
            if not backup.is_file() or sha256(backup) != row["previous_sha256"]:
                raise ValueError(f"backup missing or changed: {backup}")
            action = "restore_backup"
        else:
            action = "leave_unchanged"
        actions.append({"relative_path": row["relative_path"], "action": action})
    if apply:
        for row, action in zip(rows, actions):
            target = root / row["relative_path"]
            if action["action"] == "delete_created_file":
                target.unlink()
            elif action["action"] == "restore_backup":
                atomic_copy(Path(row["backup_path"]), target)
        directory = root / "algorithms"
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    return {
        "operation": "rollback_policy", "platform_root": str(root),
        "dry_run": not apply, "actions": actions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = rollback(args.report, apply=args.apply)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
