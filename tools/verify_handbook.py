#!/usr/bin/env python3
"""Verify PDF structure, Chinese text, embedded fonts, and rasterization."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


REQUIRED_TEXT = (
    "Agent Memory v3.1",
    "不中斷",
    "WORKING DECISIONS",
    "English paraphrase held-out Hit@3 70%",
    "foreign-scope intrusion 0/20",
    "E5-small 的 English／cross-language Hit@3 是 35%／85%",
    "sealed held-out 未開封",
    "structured-passage follow-up 排除 identity/Evidence",
    "否證 identity noise 是主要 recall 成因",
    "shortlist K=32 的 English 理論上限只有 75%",
    "先拒絕 reranker-only 實驗",
    "A/B status: COMPLETE MINIMUM PILOT",
    "memory-on 11/12",
    "Protocol 4",
    "Raw benchmark JSON",
)


def _run(command: list[str]) -> str:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"{' '.join(command)} failed: {detail}")
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--preview-dir", type=Path)
    args = parser.parse_args()
    pdf = args.pdf.expanduser().resolve()
    if not pdf.is_file() or pdf.read_bytes()[:5] != b"%PDF-":
        raise SystemExit(f"not a readable PDF: {pdf}")
    for executable in ("pdfinfo", "pdffonts", "pdftotext", "pdftoppm"):
        if not shutil.which(executable):
            raise SystemExit(f"required verifier is missing: {executable}")

    info = _run(["pdfinfo", str(pdf)])
    pages_match = re.search(r"^Pages:\s+(\d+)$", info, re.MULTILINE)
    size_match = re.search(r"^Page size:\s+([\d.]+) x ([\d.]+) pts", info, re.MULTILINE)
    if not pages_match or int(pages_match.group(1)) < 12:
        raise RuntimeError("handbook has fewer than 12 pages or unreadable page metadata")
    if not size_match:
        raise RuntimeError("handbook page size is missing")
    width, height = float(size_match.group(1)), float(size_match.group(2))
    if abs(width - 595.28) > 2 or abs(height - 841.89) > 2:
        raise RuntimeError(f"handbook is not A4: {width} x {height} points")

    if not shutil.which("fc-match"):
        raise RuntimeError("fontconfig fc-match is required to verify the CJK font family")
    resolved_sans = _run(
        ["fc-match", "-f", "%{family}", "Noto Sans CJK TC"]
    ).strip()
    resolved_mono = _run(
        ["fc-match", "-f", "%{family}", "Noto Sans Mono CJK TC"]
    ).strip()
    if "Noto Sans CJK TC" not in resolved_sans:
        raise RuntimeError(f"Noto Sans CJK TC did not resolve: {resolved_sans}")
    if "Noto Sans Mono CJK TC" not in resolved_mono:
        raise RuntimeError(f"Noto Sans Mono CJK TC did not resolve: {resolved_mono}")

    fonts = _run(["pdffonts", str(pdf)])
    font_rows = [
        match.groups()
        for line in fonts.splitlines()[2:]
        if (
            match := re.search(
                r"\s+(yes|no)\s+(yes|no)\s+(yes|no)\s+\d+\s+\d+$", line
            )
        )
    ]
    if not font_rows:
        raise RuntimeError("pdffonts found no parseable embedded font rows")
    if any(embedded != "yes" for embedded, _, _ in font_rows):
        raise RuntimeError("at least one PDF font is not embedded")
    if any(unicode_map != "yes" for _, _, unicode_map in font_rows):
        raise RuntimeError("at least one PDF font lacks a Unicode map")

    with tempfile.TemporaryDirectory(prefix="agent-memory-pdf-verify-") as temporary:
        base = Path(temporary)
        text_path = base / "handbook.txt"
        _run(["pdftotext", "-layout", str(pdf), str(text_path)])
        extracted = text_path.read_text(encoding="utf-8", errors="replace")
        missing = [item for item in REQUIRED_TEXT if item not in extracted]
        if missing:
            raise RuntimeError("required handbook text missing: " + ", ".join(missing))
        if "\ufffd" in extracted:
            raise RuntimeError("PDF text extraction contains replacement characters")
        cjk_characters = len(re.findall(r"[\u3400-\u9fff]", extracted))
        if cjk_characters < 8_000:
            raise RuntimeError(
                f"too little extractable Traditional Chinese text: {cjk_characters} chars"
            )

        preview_root = (
            args.preview_dir.expanduser().resolve()
            if args.preview_dir
            else base / "preview"
        )
        preview_root.mkdir(parents=True, exist_ok=True)
        page_count = int(pages_match.group(1))
        preview_pages = sorted({1, 2, min(5, page_count), page_count})
        previews: list[str] = []
        for page in preview_pages:
            prefix = preview_root / f"page-{page:03d}"
            _run(
                [
                    "pdftoppm",
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    "-singlefile",
                    "-png",
                    "-r",
                    "96",
                    str(pdf),
                    str(prefix),
                ]
            )
            rendered = prefix.with_suffix(".png")
            if not rendered.is_file() or rendered.stat().st_size < 10_000:
                raise RuntimeError(f"page {page} did not rasterize correctly")
            previews.append(str(rendered))

        report = {
            "pdf": str(pdf),
            "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
            "bytes": pdf.stat().st_size,
            "pages": page_count,
            "page_size_points": [width, height],
            "extractable_cjk_characters": cjk_characters,
            "font_rows": len(font_rows),
            "fonts_embedded_with_unicode_maps": True,
            "resolved_sans_family": resolved_sans,
            "resolved_mono_family": resolved_mono,
            "required_text_present": True,
            "rasterized_pages": preview_pages,
        }
        if args.preview_dir:
            report["preview_files"] = previews
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
