"""Read-only exact-hash status check."""
from __future__ import annotations

import argparse
import sys

try:
    from .patch_io import InstallError, inspect_platform, print_json, resolve_platform_root
except ImportError:
    from patch_io import InstallError, inspect_platform, print_json, resolve_platform_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify ZR10 platform patch state")
    parser.add_argument("--platform-root", required=True, help="ZR10 platform project root")
    args = parser.parse_args(argv)
    try:
        status = inspect_platform(resolve_platform_root(args.platform_root))
    except (InstallError, OSError, ValueError) as exc:
        print_json({"operation": "verify", "ok": False, "error": str(exc)})
        return 2
    print_json({"operation": "verify", "ok": status["state"] in ("original", "patched"), **status})
    return 0 if status["state"] in ("original", "patched") else 2


if __name__ == "__main__":
    sys.exit(main())
