# Figures 3 and 4 publication package

This folder contains the approved envelope-based redesign of Figures 3 and 4.
It is self-contained: the plotting scripts read only the files under
`plotting/data/` and write PDF, SVG, and 600 dpi PNG files to `figures/`.

## Render

From `plotting/scripts/` run:

```powershell
python plot_figure3.py
python plot_figure4.py
```

## Data

- `plotting/data/` contains the frozen source files used by the scripts.
- `data_exports/` contains compact, human-readable figure-level tables,
  including the sampled quantile envelopes and density curves.
- `tools/export_plot_data.py` regenerates the compact tables from the frozen
  source files.

## Statistical meaning of the shaded areas

- The light and dark ribbons in the categorical panels are the 10th--90th
  percentile and interquartile envelopes of the run-level observations.
- The pale silhouettes are normalized Gaussian-kernel descriptions of the
  observed one-dimensional distributions.
- Neither type of shading is a confidence region. Frozen 95% bootstrap
  confidence intervals are the separately capped black intervals.

## Frozen results

No experimental value or frozen confidence interval was recomputed or changed
during the redesign. Curve interpolation is used only to connect categorical
summary values visually.
