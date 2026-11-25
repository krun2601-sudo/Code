"""I/O helpers shared by the reproducible figure scripts."""

from __future__ import annotations

import csv
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
PLOTTING_ROOT = HERE.parent
DATA_DIR = PLOTTING_ROOT / "data"
OUTPUT_DIR = PLOTTING_ROOT.parent / "figures"


def read_csv(name: str) -> list[dict[str, str]]:
    with (DATA_DIR / name).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(name: str):
    with (DATA_DIR / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_all(fig, stem: str, dpi: int = 600) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "svg", "png"):
        kwargs = {"dpi": dpi} if ext == "png" else {}
        fig.savefig(OUTPUT_DIR / f"{stem}.{ext}", **kwargs)

