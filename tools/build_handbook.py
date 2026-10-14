#!/usr/bin/env python3
"""Build the self-contained Traditional Chinese handbook as HTML and PDF."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unicodedata
from urllib.parse import quote

from markdown_it import MarkdownIt

try:
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_by_name
    from pygments.lexers.special import TextLexer
except ImportError:  # pragma: no cover - build-only optional fallback
    highlight = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHAPTERS = (
    ("readme", PROJECT_ROOT / "README.md", "操作手冊"),
    ("architecture", PROJECT_ROOT / "docs" / "ARCHITECTURE.md", "架構與設計論證"),
    ("schema", PROJECT_ROOT / "docs" / "SCHEMA.md", "Schema 與完整性規則"),
    ("evaluation", PROJECT_ROOT / "docs" / "EVALUATION.md", "評估與效益界線"),
    ("protocol", PROJECT_ROOT / "MEMORY_PROTOCOL.md", "Agent 操作協定"),
)
KNOWN_CHAPTERS = {path.resolve(): key for key, path, _ in CHAPTERS}
RAW_RESULT = PROJECT_ROOT / "docs" / "evaluation-results.json"
CSS_PATH = PROJECT_ROOT / "docs" / "handbook.css"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "dist" / "agent-memory-v3.1-handbook-zh-TW.pdf",
    )
    parser.add_argument("--html-output", type=Path)
    parser.add_argument("--html-only", action="store_true")
    parser.add_argument("--desktop-copy", type=Path)
    return parser.parse_args()


def _slug(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    value = re.sub(r"[^\w\u3400-\u9fff]+", "-", normalized, flags=re.UNICODE)
    return value.strip("-") or "section"


def _data_uri(path: Path) -> str:
    mime = {
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(path.suffix.casefold())
    if not mime:
        raise RuntimeError(f"unsupported handbook image type: {path}")
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def _local_target(source: Path, target: str) -> Path:
    return (source.parent / target.split("#", 1)[0]).resolve()


def _render_document(
    markdown: MarkdownIt,
    key: str,
    source: Path,
) -> tuple[str, list[dict[str, str | int]]]:
    tokens = markdown.parse(source.read_text(encoding="utf-8"))
    headings: list[dict[str, str | int]] = []
    used: dict[str, int] = {}
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            level = int(token.tag[1:])
            title = tokens[index + 1].content
            base = _slug(title)
            used[base] = used.get(base, 0) + 1
            suffix = "" if used[base] == 1 else f"-{used[base]}"
            anchor = f"{key}-{base}{suffix}"
            token.attrSet("id", anchor)
            headings.append({"level": level, "title": title, "anchor": anchor})
    nested_tokens = [
        child
        for token in tokens
        for child in ([token] + (token.children or []))
    ]
    for token in nested_tokens:
        if token.type == "image":
            target = token.attrGet("src") or ""
            if target and not re.match(r"^[a-z]+:", target):
                image_path = _local_target(source, target)
                if not image_path.is_file():
                    raise RuntimeError(f"handbook image is missing: {image_path}")
                token.attrSet("src", _data_uri(image_path))
        elif token.type == "link_open":
            target = token.attrGet("href") or ""
            if not target or re.match(r"^(?:https?|mailto|data):", target):
                continue
            if target.startswith("#"):
                token.attrSet("href", f"#{key}-{_slug(target[1:])}")
                continue
            local = _local_target(source, target)
            if local in KNOWN_CHAPTERS:
                token.attrSet("href", f"#{KNOWN_CHAPTERS[local]}")
            elif local == RAW_RESULT.resolve():
                token.attrSet("href", "#raw-benchmark")
            elif local.exists():
                token.attrSet("href", local.as_uri())
    rendered = markdown.renderer.render(tokens, markdown.options, {})
    return f'<section class="chapter" id="{key}">{rendered}</section>', headings


def _fence_renderer(tokens: list[object], index: int, options: dict, env: dict) -> str:
    token = tokens[index]
    language = token.info.strip().split()[0] if token.info.strip() else "text"
    if highlight is None:
        return f"<pre><code>{html.escape(token.content)}</code></pre>\n"
    try:
        lexer = get_lexer_by_name(language)
    except Exception:
        lexer = TextLexer()
    return highlight(token.content, lexer, HtmlFormatter(nowrap=False))


def _markdown() -> MarkdownIt:
    renderer = MarkdownIt(
        "commonmark",
        {
            "html": False,
            "linkify": False,
            "typographer": False,
            "breaks": False,
        },
    ).enable("table")
    renderer.renderer.rules["fence"] = _fence_renderer
    return renderer


def _toc(entries: list[dict[str, str | int]]) -> str:
    items: list[str] = []
    for entry in entries:
        level = int(entry["level"])
        if level > 2:
            continue
        css = "level-1" if level == 1 else "level-2"
        items.append(
            f'<li class="{css}"><a href="#{html.escape(str(entry["anchor"]))}">'
            f'{html.escape(str(entry["title"]))}</a></li>'
        )
    return "<ol>" + "".join(items) + "</ol>"


def build_html() -> str:
    if not RAW_RESULT.is_file():
        raise RuntimeError(
            "evaluation result missing; run tools/benchmark_hybrid.py and publish the verified result first"
        )
    benchmark = json.loads(RAW_RESULT.read_text(encoding="utf-8"))
    retrieval = benchmark["deterministic_retrieval"]
    held_out = retrieval["held_out"]
    performance = retrieval["performance"]
    development_v2 = benchmark["development_retrieval_v2"]
    v2_results = {
        result["profile"]: result for result in development_v2["results"]
    }
    structured_follow_up = development_v2["structured_passage_follow_up"]
    candidate_ceiling = development_v2["candidate_discrimination_ceiling"]
    ab = benchmark["end_to_end_ab"]
    ab_overall = ab["valid_effect_result"]["paired"]["overall"]
    markdown = _markdown()
    chapter_html: list[str] = []
    headings: list[dict[str, str | int]] = []
    for key, source, _ in CHAPTERS:
        rendered, chapter_headings = _render_document(markdown, key, source)
        chapter_html.append(rendered)
        headings.extend(chapter_headings)

    raw_json = html.escape(
        json.dumps(benchmark, ensure_ascii=False, sort_keys=True, indent=2)
    )
    chapter_html.append(
        '<section class="chapter raw-json" id="raw-benchmark">'
        '<h1 id="raw-benchmark-json">附錄：Raw benchmark JSON</h1>'
        '<p>此附錄是產生本手冊時使用的 machine-readable 本機實測結果。</p>'
        f'<pre><code>{raw_json}</code></pre></section>'
    )
    headings.append(
        {"level": 1, "title": "附錄：Raw benchmark JSON", "anchor": "raw-benchmark-json"}
    )

    css = CSS_PATH.read_text(encoding="utf-8")
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    abstract = (
        "本手冊完整描述一套靜默、fail-open、以 outcome evidence 校準 working decision "
        "的 durable memory。10k records、160 cases 的 v3.1 deterministic suite 顯示："
        f"exact held-out Hit@1 {held_out['exact']['hit_at_1'] * 100:.0f}%，"
        f"English paraphrase held-out Hit@3 {held_out['english_paraphrase']['hit_at_3'] * 100:.0f}%，"
        f"cross-language held-out Hit@3 {held_out['cross_language']['hit_at_3'] * 100:.0f}%，"
        f"foreign-scope intrusion 0/{retrieval['foreign_scope_all_splits']['cases']}，"
        f"lexical p95 {performance['lexical_query_p95_ms']:.3f} ms。"
        "三個品質 gate 未過，因此 semantic route 維持停用。"
        "後續 v2 development-only bakeoff 中，"
        f"E5-small 的 English／cross-language Hit@3 是 "
        f"{v2_results['multilingual-e5-small']['metrics']['english_paraphrase_hit_at_3'] * 100:.0f}%／"
        f"{v2_results['multilingual-e5-small']['metrics']['cross_language_hit_at_3'] * 100:.0f}%，"
        f"Jina v2 zh 是 "
        f"{v2_results['jina-v2-base-zh']['metrics']['english_paraphrase_hit_at_3'] * 100:.0f}%／"
        f"{v2_results['jina-v2-base-zh']['metrics']['cross_language_hit_at_3'] * 100:.0f}%；"
        "兩者都沒有 winner，sealed held-out 未開封。"
        "單一 structured-passage follow-up 排除 identity/Evidence 後，"
        f"English／cross-language 仍為 "
        f"{structured_follow_up['metrics']['english_paraphrase_hit_at_3'] * 100:.0f}%／"
        f"{structured_follow_up['metrics']['cross_language_hit_at_3'] * 100:.0f}%，"
        "因此否證 identity noise 是主要 recall 成因。"
        "零推論 candidate ceiling 接著證明，K=32 的 English oracle Hit@3 上限只有 "
        f"{candidate_ceiling['raw_semantic_oracle_ceiling']['english_paraphrase']['k32_hit_at_3_ceiling'] * 100:.0f}%，"
        "所以先拒絕 reranker-only 實驗；本步新增模型 calls 0。"
        "A/B status: COMPLETE MINIMUM PILOT；隔離的 GPT-5.6 Sol max 12-pair pilot "
        f"是 memory-on {ab_overall['memory_on_passes']}/12、"
        f"off {ab_overall['memory_off_passes']}/12，absolute delta "
        f"+{ab_overall['absolute_pass_rate_delta'] * 100:.2f}pp。"
        "每個 case/arm 只有一次，這是方向性 evidence，不是穩定 population effect。"
    )
    return f"""<!doctype html>
<html lang="zh-Hant-TW">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent Memory v3.1：非侵入式動態記憶系統</title>
  <style>{css}</style>
</head>
<body>
  <section class="cover">
    <div>
      <div class="eyebrow">Architecture · Protocol · Evaluation</div>
      <h1>Agent Memory v3.1</h1>
      <div class="subtitle">非侵入式、可修正、以實際後果校準的 Durable Memory</div>
    </div>
    <div class="thesis">決定性規則只保護資料與 liveness；機率性的過去經驗保留為證據，
    永遠不能取代目前使用者指示與新鮮查證。</div>
    <div class="meta"><span>繁體中文完整手冊</span><span>Build {generated}</span></div>
  </section>

  <section class="frontmatter">
    <h1>摘要與目錄</h1>
    <div class="abstract">{html.escape(abstract)}</div>
    <div class="build-note">本文件由 repository Markdown、CSS、SVG 與 raw benchmark JSON
    離線建置。效能數字是目前機器的單次可重現 run；不是跨機保證。Retrieval 分數不等於
    LLM 最終決策品質。</div>
    <nav class="toc">{_toc(headings)}</nav>
  </section>

  {''.join(chapter_html)}
</body>
</html>
"""


def _chrome() -> str:
    explicit = os.environ.get("AGENT_MEMORY_CHROME")
    if explicit:
        return explicit
    for name in ("google-chrome", "chromium", "chromium-browser"):
        candidate = shutil.which(name)
        if candidate:
            return candidate
    raise RuntimeError("Google Chrome or Chromium is required to build the PDF")


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _copy_with_backup(source: Path, destination: Path) -> tuple[Path | None, str]:
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if destination.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = destination.with_name(
            f"{destination.stem}.backup-{stamp}{destination.suffix}"
        )
        counter = 1
        while backup.exists():
            backup = destination.with_name(
                f"{destination.stem}.backup-{stamp}-{counter}{destination.suffix}"
            )
            counter += 1
        shutil.copy2(destination, backup)
    _write_atomic(destination, source.read_bytes())
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    destination_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
    if source_hash != destination_hash:
        raise RuntimeError("Desktop PDF checksum mismatch after copy")
    return backup, destination_hash


def main() -> int:
    args = _arguments()
    html_text = build_html()
    output = args.output.expanduser().resolve()
    html_output = (
        args.html_output.expanduser().resolve()
        if args.html_output
        else output.with_suffix(".html")
    )
    _write_atomic(html_output, html_text.encode("utf-8"))
    if args.html_only:
        print(f"HTML handbook created at {html_output}")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agent-memory-pdf-") as temporary:
        temporary_pdf = Path(temporary) / output.name
        command = [
            _chrome(),
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--allow-file-access-from-files",
            "--no-pdf-header-footer",
            f"--print-to-pdf={temporary_pdf}",
            html_output.as_uri(),
        ]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode != 0 or not temporary_pdf.is_file():
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"Chrome PDF build failed: {detail}")
        _write_atomic(output, temporary_pdf.read_bytes())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(f"PDF handbook created at {output}")
    print(f"SHA-256 {digest}")
    if args.desktop_copy:
        backup, copied_digest = _copy_with_backup(output, args.desktop_copy)
        if backup:
            print(f"Previous Desktop PDF preserved at {backup}")
        print(f"Checksum-verified Desktop copy: {args.desktop_copy.expanduser().resolve()}")
        print(f"Desktop SHA-256 {copied_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
