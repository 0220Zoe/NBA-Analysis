#!/usr/bin/env python3
"""Create a simple, dependency-free PDF from INTERVIEW_PREP.md."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "INTERVIEW_PREP.md"
OUTPUT = ROOT / "INTERVIEW_PREP.pdf"

PAGE_WIDTH = 612
PAGE_HEIGHT = 792
LEFT = 54
TOP = 744
BOTTOM = 48
BODY_SIZE = 9.5
BODY_LEADING = 13


def ascii_safe(text: str) -> str:
    replacements = {
        "—": "-", "–": "-", "’": "'", "‘": "'", "“": '"', "”": '"',
        "→": "->", "←": "<-", "≥": ">=", "≤": "<=", "×": "x",
        "•": "-", "…": "...", "²": "2",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text.encode("latin-1", "replace").decode("latin-1")


def wrap(text: str, width: int) -> list[str]:
    if not text:
        return [""]
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 <= width:
            current = f"{current} {word}".strip()
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def markdown_lines(markdown: str) -> list[tuple[str, float, float, str]]:
    """Return (text, font size, leading, style) tuples."""
    result: list[tuple[str, float, float, str]] = []
    in_code = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            result.append(("", BODY_SIZE, BODY_LEADING, "body"))
            continue
        if in_code:
            for part in wrap(line, 92):
                result.append((part, 8.1, 10.5, "code"))
            continue
        if not line.strip():
            result.append(("", BODY_SIZE, 7, "body"))
            continue
        if line.startswith("# "):
            result.append((line[2:], 20, 25, "title"))
        elif line.startswith("## "):
            result.append((line[3:], 14, 19, "h2"))
        elif line.startswith("### "):
            result.append((line[4:], 11.5, 15, "h3"))
        elif line.startswith("|---") or re.fullmatch(r"\|[-| :]+\|", line):
            result.append(("-" * 88, 6.5, 8, "rule"))
        elif line.startswith("| "):
            cells = [c.strip() for c in line.strip("|").split("|")]
            result.append(("  |  ".join(cells), 8.2, 11, "table"))
        elif line.startswith("- "):
            for i, part in enumerate(wrap(line[2:], 91)):
                result.append((("- " if i == 0 else "  ") + part, BODY_SIZE, BODY_LEADING, "body"))
        elif re.match(r"^\d+\. ", line):
            prefix, text = line.split(" ", 1)
            parts = wrap(text, 87)
            result.append((prefix + " " + parts[0], BODY_SIZE, BODY_LEADING, "body"))
            for part in parts[1:]:
                result.append(("  " + part, BODY_SIZE, BODY_LEADING, "body"))
        else:
            for part in wrap(line, 96):
                result.append((part, BODY_SIZE, BODY_LEADING, "body"))
    return result


def pdf_escape(text: str) -> str:
    return ascii_safe(text).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(lines: list[tuple[str, float, float, str]]) -> bytes:
    pages: list[list[tuple[str, float, float, str]]] = []
    page: list[tuple[str, float, float, str]] = []
    y = TOP
    for item in lines:
        text, size, leading, style = item
        if y - leading < BOTTOM and page:
            pages.append(page)
            page = []
            y = TOP
        page.append(item)
        y -= leading
    if page:
        pages.append(page)

    objects: list[bytes] = []
    def add(obj: str | bytes) -> int:
        objects.append(obj.encode("latin-1") if isinstance(obj, str) else obj)
        return len(objects)

    catalog_id = add("")
    pages_id = add("")
    font_id = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    content_ids: list[int] = []

    for page_lines in pages:
        commands = ["BT"]
        y = TOP
        for text, size, leading, style in page_lines:
            if style == "title":
                commands.append("0.08 0.18 0.32 rg")
            elif style in {"h2", "h3"}:
                commands.append("0.12 0.32 0.48 rg")
            elif style == "code":
                commands.append("0.15 0.15 0.15 rg")
            else:
                commands.append("0 0 0 rg")
            commands.append(f"/F1 {size:g} Tf 1 0 0 1 {LEFT} {y:g} Tm ({pdf_escape(text)}) Tj")
            y -= leading
        commands.append("ET")
        stream = "\n".join(commands).encode("latin-1")
        content_ids.append(add(f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream"))
        page_ids.append(add(""))

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[pages_id - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("latin-1")
    for pid, cid in zip(page_ids, content_ids):
        objects[pid - 1] = (
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {cid} 0 R >>"
        ).encode("latin-1")
    objects[catalog_id - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("latin-1")

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("latin-1"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("latin-1")
    )
    return bytes(output)


def main() -> None:
    OUTPUT.write_bytes(build_pdf(markdown_lines(SOURCE.read_text(encoding="utf-8"))))
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
