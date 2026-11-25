# ECR-Cover AIS submission package

## Primary deliverables

- `ECR_Cover_AIS_Overleaf.zip` — clean Overleaf project based on the Wiley Optimal Design, Two-Column (June 2026) template.
- `ECR_Cover_AIS_English.pdf` — locally compiled 13-page English manuscript.
- `ECR_Cover_AIS_Chinese_Review.docx` — section-aligned Chinese review manuscript with native Word equations.
- `ECR_Cover_AIS_Chinese_Review.pdf` — rendered review copy used for page-level QA.
- `ECR_Cover_AIS_Figures_and_Code.zip` — reproducible plotting code, frozen data inputs, PDF/SVG vectors, and 600 dpi PNG files.
- `reports/AIS_restructuring_and_audit.md` — structure diagnosis, technical-fidelity audit, claim audit, and AI-writing audit.
- `qa/fidelity_validation.json` — machine-generated fidelity check result.

## Overleaf compilation

Upload `ECR_Cover_AIS_Overleaf.zip`, select `main.tex` as the main document, and compile with pdfLaTeX. The project bundles the Wiley class, bibliography style, fonts, and figure PDFs used in the local build.

## Figure regeneration

From the extracted figures/code package, run:

```powershell
python plotting/scripts/render_all.py
```

The scripts read only from `plotting/data` and write PDF, SVG, and PNG outputs to `figures`.

## Author actions required before submission

Replace every visible placeholder for author names, affiliations, corresponding author, funding, acknowledgments, author contributions, conflict of interest, data availability/repository link, and generative-AI use disclosure. These fields were intentionally not inferred or fabricated.

The technical source remains `../ral_cn_final.md`. Do not silently replace frozen experimental values or confidence intervals when editing prose or figures.

