"""Regenerate every data figure used in the manuscript."""
from __future__ import annotations

from .plot_nominal_results import plot as plot_nominal
from .plot_repair_results import plot as plot_repair


def main() -> None:
    for output in (plot_nominal(), plot_repair()):
        print(output)


if __name__ == "__main__":
    main()

