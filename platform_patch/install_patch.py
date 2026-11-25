"""Dry-run by default; pass --apply only after reviewing the result."""
from __future__ import annotations

import argparse
import sys

try:
    from .patch_io import InstallError, install, print_json, resolve_platform_root
except ImportError:
    from patch_io import InstallError, install, print_json, resolve_platform_root


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Install the exact ZR10 4.0.0 RA-L HIL context patch")
    result.add_argument("--platform-root", required=True, help="ZR10 platform project root")
    result.add_argument("--python", help="platform virtual-environment Python executable")
    result.add_argument("--full-tests", action="store_true", help="run the platform pytest suite against the overlay")
    result.add_argument("--apply", action="store_true", help="commit after hash gate and preflight (default: dry-run)")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = install(resolve_platform_root(args.platform_root), apply=args.apply,
                         python=args.python, full_tests=args.full_tests)
    except (InstallError, OSError, ValueError) as exc:
        print_json({"operation": "install", "ok": False, "error": str(exc)})
        return 2
    print_json({"ok": True, **result})
    return 0


if __name__ == "__main__":
    sys.exit(main())

