"""Build the Chinese hardware-validation manuscript as a styled DOCX.

Pandoc is used only for Markdown/LaTeX-to-Word conversion so block and inline
equations become editable Office Math.  python-docx then applies a compact,
review-friendly academic style.
"""
from __future__ import annotations

import re
from pathlib import Path

import pypandoc
from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "ral_cn_preexperiment.md"
OUTPUT = ROOT / "ral_cn_hardware_validation.docx"
TMP = ROOT / ".ral_cn_hardware_validation.pandoc.docx"

TITLE_CN = "网络化云台相机的执行确认协同视场覆盖"
TITLE_EN = "Execution-Confirmed Cooperative Field-of-View Coverage for Networked Pan–Tilt Cameras"


def set_fonts(run, latin: str = "Times New Roman", east_asia: str = "宋体") -> None:
    run.font.name = latin
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.rFonts
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), latin)
    rfonts.set(qn("w:hAnsi"), latin)
    rfonts.set(qn("w:eastAsia"), east_asia)


def set_style_font(style, size: float, bold: bool = False,
                   latin: str = "Times New Roman", east_asia: str = "宋体") -> None:
    style.font.name = latin
    style.font.size = Pt(size)
    style.font.bold = bold
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.rFonts
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), latin)
    rfonts.set(qn("w:hAnsi"), latin)
    rfonts.set(qn("w:eastAsia"), east_asia)


def shade_cell(cell, fill: str) -> None:
    tcpr = cell._tc.get_or_add_tcPr()
    shd = tcpr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tcpr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_repeat_table_header(row) -> None:
    trpr = row._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    trpr.append(tbl_header)


def set_cell_margins(cell, top=55, start=70, bottom=55, end=70) -> None:
    tc = cell._tc
    tcpr = tc.get_or_add_tcPr()
    tcmar = tcpr.first_child_found_in("w:tcMar")
    if tcmar is None:
        tcmar = OxmlElement("w:tcMar")
        tcpr.append(tcmar)
    for margin, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tcmar.find(qn(f"w:{margin}"))
        if node is None:
            node = OxmlElement(f"w:{margin}")
            tcmar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_table_borders(table, color="9FB3C8", size="4") -> None:
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


def add_page_field(paragraph) -> None:
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run("— ")
    set_fonts(run)
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
    tail = paragraph.add_run(" —")
    set_fonts(tail)


def add_bottom_border(paragraph, color="9FB3C8", size="6") -> None:
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


def highlight_placeholders(paragraph) -> None:
    marker = re.compile(r"\[(?:待实测|待填写|自动生成|完成日志校验后自动写入)\]")
    for run in list(paragraph.runs):
        if not marker.search(run.text):
            continue
        text = run.text
        parent = run._r.getparent()
        idx = parent.index(run._r)
        parent.remove(run._r)
        cursor = 0
        for match in marker.finditer(text):
            if match.start() > cursor:
                normal = OxmlElement("w:r")
                t = OxmlElement("w:t")
                t.text = text[cursor:match.start()]
                normal.append(t)
                parent.insert(idx, normal)
                idx += 1
            marked = OxmlElement("w:r")
            rpr = OxmlElement("w:rPr")
            color = OxmlElement("w:color")
            color.set(qn("w:val"), "9C0006")
            highlight = OxmlElement("w:highlight")
            highlight.set(qn("w:val"), "yellow")
            bold = OxmlElement("w:b")
            rpr.extend([color, highlight, bold])
            marked.append(rpr)
            t = OxmlElement("w:t")
            t.text = match.group(0)
            marked.append(t)
            parent.insert(idx, marked)
            idx += 1
            cursor = match.end()
        if cursor < len(text):
            normal = OxmlElement("w:r")
            t = OxmlElement("w:t")
            t.text = text[cursor:]
            normal.append(t)
            parent.insert(idx, normal)


def style_document(path: Path) -> None:
    doc = Document(path)
    sec = doc.sections[0]
    sec.page_width = Cm(21.0)
    sec.page_height = Cm(29.7)
    sec.top_margin = Cm(1.65)
    sec.bottom_margin = Cm(1.55)
    sec.left_margin = Cm(1.75)
    sec.right_margin = Cm(1.75)
    sec.header_distance = Cm(0.65)
    sec.footer_distance = Cm(0.65)

    styles = doc.styles
    set_style_font(styles["Normal"], 10.2)
    styles["Normal"].paragraph_format.line_spacing = 1.18
    styles["Normal"].paragraph_format.space_after = Pt(3.5)
    styles["Normal"].paragraph_format.first_line_indent = Cm(0.74)

    set_style_font(styles["Title"], 18, True, east_asia="微软雅黑")
    styles["Title"].font.color.rgb = RGBColor(0, 0, 0)
    styles["Title"].paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    styles["Title"].paragraph_format.space_after = Pt(5)

    for name, size, before, after in (
        ("Heading 1", 13.2, 10, 4),
        ("Heading 2", 11.3, 7, 3),
        ("Heading 3", 10.4, 5, 2),
    ):
        if name not in styles:
            continue
        set_style_font(styles[name], size, True, east_asia="微软雅黑")
        styles[name].font.color.rgb = RGBColor(0, 0, 0)
        styles[name].paragraph_format.space_before = Pt(before)
        styles[name].paragraph_format.space_after = Pt(after)
        styles[name].paragraph_format.keep_with_next = True
        styles[name].paragraph_format.first_line_indent = Cm(0)

    if "Caption" in styles:
        set_style_font(styles["Caption"], 8.6)
        styles["Caption"].font.color.rgb = RGBColor(45, 45, 45)
        styles["Caption"].paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
        styles["Caption"].paragraph_format.space_before = Pt(2)
        styles["Caption"].paragraph_format.space_after = Pt(5)
        styles["Caption"].paragraph_format.first_line_indent = Cm(0)

    # The first H1 is the document title; following H1 paragraphs are sections.
    for p in doc.paragraphs:
        if p.text.strip() == TITLE_CN:
            p.style = styles["Title"]
            break

    # Front-matter alignment and compact abstract treatment.
    for p in doc.paragraphs:
        text = p.text.strip()
        if text == TITLE_EN:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.first_line_indent = Cm(0)
            p.paragraph_format.space_after = Pt(3)
            for r in p.runs:
                r.bold = True
                r.font.size = Pt(11)
        elif text.startswith("匿名稿"):
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.first_line_indent = Cm(0)
            p.paragraph_format.space_after = Pt(8)
            for r in p.runs:
                r.font.color.rgb = RGBColor(98, 125, 152)
        elif text.startswith("关键词：") or text.startswith("关键词:"):
            p.paragraph_format.first_line_indent = Cm(0)
        elif text == "参考文献":
            p.paragraph_format.keep_with_next = True

    # Keep the lead-in attached to its display equation and use IEEE-like
    # hanging indents for the reference list.
    in_references = False
    for p in doc.paragraphs:
        text = p.text.strip()
        if text == "主要指标为固定时限下的未覆盖面积":
            p.paragraph_format.keep_with_next = True
        if text == "参考文献":
            in_references = True
            continue
        if in_references and text:
            p.paragraph_format.left_indent = Cm(0.74)
            p.paragraph_format.first_line_indent = Cm(-0.74)
            p.paragraph_format.line_spacing = 1.0
            p.paragraph_format.space_after = Pt(4)

    # Global run fonts, paragraph controls, placeholders, and image bounds.
    for p in doc.paragraphs:
        if p.style.name not in ("Title", "Heading 1", "Heading 2", "Heading 3", "Caption"):
            p.paragraph_format.widow_control = True
        for r in p.runs:
            set_fonts(r)
        highlight_placeholders(p)
        for r in p.runs:
            for drawing in r._r.xpath(".//w:drawing"):
                for extent in drawing.xpath(".//wp:extent"):
                    cx = int(extent.get("cx"))
                    cy = int(extent.get("cy"))
                    max_cx = int(Cm(16.8).emu)
                    if cx > max_cx:
                        extent.set("cx", str(max_cx))
                        extent.set("cy", str(int(cy * max_cx / cx)))

    for table in doc.tables:
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.style = "Table Grid" if "Table Grid" in styles else "Table"
        set_table_borders(table)
        table.autofit = True
        if table.rows:
            set_repeat_table_header(table.rows[0])
        for ri, row in enumerate(table.rows):
            for cell in row.cells:
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                set_cell_margins(cell)
                if ri == 0:
                    shade_cell(cell, "D9EAF7")
                for p in cell.paragraphs:
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    p.paragraph_format.first_line_indent = Cm(0)
                    p.paragraph_format.space_after = Pt(1)
                    p.paragraph_format.line_spacing = 1.0
                    for r in p.runs:
                        set_fonts(r)
                        r.font.size = Pt(8.2)
                        if ri == 0:
                            r.bold = True
                    highlight_placeholders(p)

    # Header/footer are intentionally anonymous.
    hp = sec.header.paragraphs[0]
    hp.text = "EA-CML-H｜实物验证中文技术稿｜匿名"
    hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    hp.paragraph_format.first_line_indent = Cm(0)
    add_bottom_border(hp, color="BCCCDC", size="4")
    for r in hp.runs:
        set_fonts(r, east_asia="微软雅黑")
        r.font.size = Pt(7.5)
        r.font.color.rgb = RGBColor(98, 125, 152)
    fp = sec.footer.paragraphs[0]
    fp.clear()
    add_page_field(fp)
    for r in fp.runs:
        r.font.size = Pt(8)
        r.font.color.rgb = RGBColor(98, 125, 152)

    # Core properties and privacy-friendly metadata.
    doc.core_properties.title = TITLE_CN
    doc.core_properties.subject = "Chinese hardware-validation manuscript; execution-confirmed cooperative coverage"
    doc.core_properties.author = "Anonymous"
    doc.core_properties.comments = "Hardware commissioning results are separated from the pending coverage-method comparison."
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
