from __future__ import annotations

import argparse
import math
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw


def render(pdf_path: Path, output_dir: Path, scale: float = 1.7) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    document = pdfium.PdfDocument(str(pdf_path))
    pages: list[Path] = []
    for index in range(len(document)):
        image = document[index].render(scale=scale).to_pil().convert("RGB")
        path = output_dir / f"page_{index + 1:03d}.png"
        image.save(path, optimize=True)
        pages.append(path)
    return pages


def make_contact_sheets(page_paths: list[Path], output_dir: Path) -> list[Path]:
    sheets: list[Path] = []
    pages_per_sheet = 4
    thumb_width = 700
    margin = 35
    label_height = 38

    for sheet_index in range(math.ceil(len(page_paths) / pages_per_sheet)):
        batch = page_paths[sheet_index * pages_per_sheet : (sheet_index + 1) * pages_per_sheet]
        thumbs: list[Image.Image] = []
        for path in batch:
            image = Image.open(path).convert("RGB")
            height = round(image.height * thumb_width / image.width)
            thumbs.append(image.resize((thumb_width, height), Image.Resampling.LANCZOS))

        cell_height = max(image.height for image in thumbs) + label_height
        canvas = Image.new(
            "RGB",
            (2 * thumb_width + 3 * margin, 2 * cell_height + 3 * margin),
            "white",
        )
        draw = ImageDraw.Draw(canvas)
        for item_index, (path, image) in enumerate(zip(batch, thumbs)):
            row, column = divmod(item_index, 2)
            x = margin + column * (thumb_width + margin)
            y = margin + row * (cell_height + margin)
            draw.text((x, y), path.stem.replace("page_", "Page "), fill="black")
            canvas.paste(image, (x, y + label_height))

        sheet_path = output_dir / f"contact_{sheet_index + 1:02d}.png"
        canvas.save(sheet_path, optimize=True)
        sheets.append(sheet_path)
    return sheets


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()

    pages = render(args.pdf.resolve(), args.output_dir.resolve())
    sheets = make_contact_sheets(pages, args.output_dir.resolve())
    print(f"rendered_pages={len(pages)}")
    print(f"contact_sheets={len(sheets)}")


if __name__ == "__main__":
    main()
