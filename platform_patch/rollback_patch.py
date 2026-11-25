"""Verified, transactional rollback for the RA-L HIL platform patch."""
from __future__ import annotations

import argparse
import sys

try:
    from .patch_io import InstallError, print_json, resolve_platform_root, rollback
except ImportError:
    from patch_io import InstallError, print_json, resolve_platform_root, rollback


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Rollback the ZR10 RA-L HIL context patch")
    result.add_argument("--platform-root", required=True, help="ZR10 platform project root")
    result.add_argument("--backup-id", help="specific backup directory name (default: newest installed backup)")
    result.add_argument("--apply", action="store_true", help="commit rollback (default: dry-run)")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = rollback(resolve_platform_root(args.platform_root), apply=args.apply,
                          backup_id=args.backup_id)
    except (InstallError, OSError, ValueError) as exc:
        print_json({"operation": "rollback", "ok": False, "error": str(exc)})
        return 2
    print_json({"ok": True, **result})
    return 0


if __name__ == "__main__":
    sys.exit(main())

