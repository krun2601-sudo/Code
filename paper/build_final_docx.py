"""Build and style the final Chinese RA-L working manuscript."""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pypandoc
from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "ral_cn_final.md"
TMP = ROOT / ".ral_cn_final.pandoc.docx"
OUTPUT = ROOT / "ral_cn_final_safety_core_revised.docx"

TITLE_CN = "执行确认与非退化修复驱动的网络化定向相机协同覆盖"
TITLE_EN = "Execution-Confirmed Cooperative Coverage with Non-Degrading Repair for Networked Directional Cameras"

NAVY = "173F5F"
PALE_BLUE = "DCEAF4"
GRID = "AEBCC8"


def set_fonts(run, *, latin: str = "Times New Roman", east_asia: str = "宋体") -> None:
    run.font.name = latin
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.rFonts
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), latin)
    rfonts.set(qn("w:hAnsi"), latin)
    rfonts.set(qn("w:eastAsia"), east_asia)


def set_style_font(style, size: float, *, bold: bool = False, east_asia: str = "宋体") -> None:
    style.font.name = "Times New Roman"
    style.font.size = Pt(size)
    style.font.bold = bold
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.rFonts
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), "Times New Roman")
    rfonts.set(qn("w:hAnsi"), "Times New Roman")
    rfonts.set(qn("w:eastAsia"), east_asia)


def shade_cell(cell, fill: str) -> None:
    tcpr = cell._tc.get_or_add_tcPr()
    shd = tcpr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tcpr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_table_borders(table, color=GRID, size="4") -> None:
    tblpr = table._tbl.tblPr
    borders = tblpr.find(qn("w:tblBorders"))
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        tblpr.append(borders)
    for name in ("top", "left", "bottom", "right", "insideH", "insideV"):
        edge = borders.find(qn(f"w:{name}"))
        if edge is None:
            edge = OxmlElement(f"w:{name}")
            borders.append(edge)
        edge.set(qn("w:val"), "single")
        edge.set(qn("w:sz"), size)
        edge.set(qn("w:space"), "0")
        edge.set(qn("w:color"), color)


def set_cell_margins(cell, top=45, start=55, bottom=45, end=55) -> None:
    tcpr = cell._tc.get_or_add_tcPr()
    margins = tcpr.find(qn("w:tcMar"))
    if margins is None:
        margins = OxmlElement("w:tcMar")
        tcpr.append(margins)
    for name, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = margins.find(qn(f"w:{name}"))
        if node is None:
            node = OxmlElement(f"w:{name}")
            margins.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def repeat_header(row) -> None:
    trpr = row._tr.get_or_add_trPr()
    hdr = OxmlElement("w:tblHeader")
    hdr.set(qn("w:val"), "true")
    trpr.append(hdr)


def keep_row_together(row) -> None:
    trpr = row._tr.get_or_add_trPr()
    cant_split = OxmlElement("w:cantSplit")
    trpr.append(cant_split)


def add_page_number(paragraph) -> None:
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run("")
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend([begin, instr, separate, text, end])
    set_fonts(run)


def add_bottom_border(paragraph, color=GRID, size="4") -> None:
    ppr = paragraph._p.get_or_add_pPr()
    pbdr = ppr.find(qn("w:pBdr"))
    if pbdr is None:
        pbdr = OxmlElement("w:pBdr")
        ppr.append(pbdr)
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), size)
    bottom.set(qn("w:space"), "2")
    bottom.set(qn("w:color"), color)
    pbdr.append(bottom)


def set_repeat_heading(paragraph) -> None:
    paragraph.paragraph_format.keep_with_next = True
    paragraph.paragraph_format.keep_together = True


def normalize_image_bounds(doc: Document) -> None:
    max_width = int(Cm(17.65).emu)
    for paragraph in doc.paragraphs:
        for drawing in paragraph._p.xpath(".//w:drawing"):
            for extent in drawing.xpath(".//wp:extent"):
                cx, cy = int(extent.get("cx")), int(extent.get("cy"))
                if cx > max_width:
                    extent.set("cx", str(max_width))
                    extent.set("cy", str(round(cy * max_width / cx)))
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.first_line_indent = Cm(0)
            paragraph.paragraph_format.space_before = Pt(2)
            paragraph.paragraph_format.space_after = Pt(1)


def style_document(path: Path) -> None:
    doc = Document(path)
    section = doc.sections[0]
    section.page_width = Cm(21.59)
    section.page_height = Cm(27.94)
    section.top_margin = Cm(1.45)
    section.bottom_margin = Cm(1.35)
    section.left_margin = Cm(1.85)
    section.right_margin = Cm(1.85)
    section.header_distance = Cm(0.55)
    section.footer_distance = Cm(0.55)

    styles = doc.styles
    set_style_font(styles["Normal"], 9.5)
    styles["Normal"].paragraph_format.line_spacing = 1.08
    styles["Normal"].paragraph_format.space_after = Pt(2.2)
    styles["Normal"].paragraph_format.first_line_indent = Cm(0.63)
    styles["Normal"].paragraph_format.widow_control = True

    set_style_font(styles["Title"], 16.5, bold=True, east_asia="微软雅黑")
    styles["Title"].paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    styles["Title"].paragraph_format.space_after = Pt(4)

    for name, size, before, after in (
        ("Heading 1", 11.4, 7.5, 2.5),
        ("Heading 2", 10.2, 5.0, 1.5),
        ("Heading 3", 9.6, 4.0, 1.0),
    ):
        if name in styles:
            set_style_font(styles[name], size, bold=True, east_asia="微软雅黑")
            styles[name].font.color.rgb = RGBColor.from_string(NAVY)
            styles[name].paragraph_format.space_before = Pt(before)
            styles[name].paragraph_format.space_after = Pt(after)
            styles[name].paragraph_format.first_line_indent = Cm(0)
            styles[name].paragraph_format.keep_with_next = True
            styles[name].paragraph_format.keep_together = True

    if "Caption" in styles:
        set_style_font(styles["Caption"], 8.0)
        styles["Caption"].font.color.rgb = RGBColor(43, 55, 66)
        styles["Caption"].paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
        styles["Caption"].paragraph_format.first_line_indent = Cm(0)
        styles["Caption"].paragraph_format.line_spacing = 1.0
        styles["Caption"].paragraph_format.space_before = Pt(1)
        styles["Caption"].paragraph_format.space_after = Pt(4)
        styles["Caption"].paragraph_format.keep_with_next = False
        styles["Caption"].paragraph_format.keep_together = True

    in_references = False
    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if text == TITLE_CN:
            paragraph.style = styles["Title"]
        elif text == TITLE_EN:
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.first_line_indent = Cm(0)
            paragraph.paragraph_format.space_after = Pt(5)
            for run in paragraph.runs:
                run.bold = True
                run.font.size = Pt(10.3)
        elif text.startswith("摘要—") or text.startswith("关键词—"):
            paragraph.paragraph_format.first_line_indent = Cm(0)
            paragraph.paragraph_format.line_spacing = 1.04
            paragraph.paragraph_format.space_after = Pt(4)
        elif re.match(r"^\*\*表\s*[IVX]+", text):
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.first_line_indent = Cm(0)
            paragraph.paragraph_format.space_before = Pt(4)
            paragraph.paragraph_format.space_after = Pt(2)
            paragraph.paragraph_format.keep_with_next = True
        elif text == "参考文献":
            in_references = True
            set_repeat_heading(paragraph)
        elif in_references and text:
            paragraph.paragraph_format.left_indent = Cm(0.57)
            paragraph.paragraph_format.first_line_indent = Cm(-0.57)
            paragraph.paragraph_format.line_spacing = 1.0
            paragraph.paragraph_format.space_after = Pt(2.2)
            for run in paragraph.runs:
                run.font.size = Pt(8.3)

        if paragraph.style.name in {"Heading 1", "Heading 2", "Heading 3"}:
            set_repeat_heading(paragraph)

        if paragraph._p.xpath(".//m:oMathPara"):
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.first_line_indent = Cm(0)
            paragraph.paragraph_format.space_before = Pt(2.5)
            paragraph.paragraph_format.space_after = Pt(3.0)
            paragraph.paragraph_format.keep_together = True

        for run in paragraph.runs:
            set_fonts(run)

    normalize_image_bounds(doc)

    for table in doc.tables:
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.autofit = True
        set_table_borders(table)
        if table.rows:
            repeat_header(table.rows[0])
        for row_index, row in enumerate(table.rows):
            keep_row_together(row)
            for cell in row.cells:
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                set_cell_margins(cell)
                if row_index == 0:
                    shade_cell(cell, PALE_BLUE)
                for paragraph in cell.paragraphs:
                    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    paragraph.paragraph_format.first_line_indent = Cm(0)
                    paragraph.paragraph_format.line_spacing = 1.0
                    paragraph.paragraph_format.space_after = Pt(0.8)
                    for run in paragraph.runs:
                        set_fonts(run)
                        run.font.size = Pt(7.6)
                        if row_index == 0:
                            run.bold = True

    header = section.header.paragraphs[0]
    header.text = "IEEE ROBOTICS AND AUTOMATION LETTERS｜中文匿名稿"
    header.alignment = WD_ALIGN_PARAGRAPH.CENTER
    header.paragraph_format.first_line_indent = Cm(0)
    add_bottom_border(header)
    for run in header.runs:
        set_fonts(run, east_asia="微软雅黑")
        run.font.size = Pt(7.1)
        run.font.color.rgb = RGBColor(79, 95, 108)

    footer = section.footer.paragraphs[0]
    footer.clear()
    add_page_number(footer)
    for run in footer.runs:
        run.font.size = Pt(7.8)
        run.font.color.rgb = RGBColor(79, 95, 108)

    doc.core_properties.title = TITLE_CN
    doc.core_properties.subject = "Chinese RA-L working manuscript on execution-confirmed cooperative coverage"
    doc.core_properties.author = "Anonymous"
    doc.core_properties.keywords = "multi-robot coverage; active vision; execution confirmation; task allocation"
    doc.core_properties.comments = "Double-anonymous Chinese working manuscript."
    doc.save(path)


def main() -> None:
    if not SOURCE.exists():
        raise SystemExit(f"missing source: {SOURCE}")
    pypandoc.convert_file(
        str(SOURCE),
        "docx",
        outputfile=str(TMP),
        extra_args=[
            "--standalone",
            f"--resource-path={ROOT}",
            "--from=markdown+tex_math_single_backslash+tex_math_dollars+raw_tex+link_attributes",
        ],
    )
    TMP.replace(OUTPUT)
    style_document(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    main()
