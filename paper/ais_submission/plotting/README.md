# Reproducible figures

All manuscript figures are generated from the scripts in `scripts/`. The two
run-level CSV files and the frozen V2.4.3 audit record in `data/` are copied
from the archived formal experiment directories. Fixed confidence intervals
shown in the forest panels are the preregistered 100000-resample bootstrap
intervals reported in the source manuscript; the plotting scripts do not
recompute or replace them.

Figures 3 and 4 additionally use descriptive shading. The nested ribbons are
the run-level 10th--90th percentile and interquartile envelopes; pale
silhouettes are normalized one-dimensional kernel-density descriptions. These
shaded elements are not confidence regions. Frozen bootstrap intervals remain
the separately capped black intervals.

Run from this directory:

```powershell
C:\Users\krun\miniconda3\python.exe scripts\render_all.py
```

Outputs are written to the sibling `figures/` directory as vector PDF, SVG,
and 600 dpi PNG files. The design widths are 178 mm for the four main figures
and 85 mm for the ToC graphic.
