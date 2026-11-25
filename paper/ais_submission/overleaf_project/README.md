# ECR-Cover — Advanced Intelligent Systems submission package

This project uses Wiley's official **Optimal Design, Two-Column (June 2026)**
LaTeX template. Compile `main.tex` with pdfLaTeX and BibTeX. The manuscript is
anonymous and intentionally retains visible placeholders for author identity,
affiliation, correspondence, acknowledgments, funding, conflicts of interest,
data availability, and generative-AI disclosure.

The `figures/` directory contains vector PDF figures. `toc_entry.tex` provides
the 55-word ToC text and a derivative ToC graphic. Reproducible Matplotlib
sources and the archived plotting inputs are delivered in the separate
`ECR_Cover_AIS_Figures_and_Code.zip` package.

Recommended local build sequence:

```text
pdflatex main.tex
bibtex main
pdflatex main.tex
pdflatex main.tex
```

No equation, experimental observation, confidence interval, or conclusion was
changed from the locked Chinese technical source. The restructuring and audit
report documents the evidence boundaries and cross-language checks.
