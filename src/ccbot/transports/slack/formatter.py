"""Slack mrkdwn formatter — converts standard Markdown to Slack's mrkdwn format.

Handles bold, italic, code (inline and block), links, headings, strikethrough,
and Markdown tables (converted to Block Kit ``table`` blocks).
Code blocks and inline code are protected from transformation via placeholders.
"""

import re
from typing import Any

from ...transcript_parser import TranscriptParser

_PLACEHOLDER_PREFIX = "\x00PH"
_BOLD_OPEN = "\x01BOLD_O\x01"
_BOLD_CLOSE = "\x01BOLD_C\x01"

_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_SEPARATOR_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")


def strip_sentinels(text: str) -> str:
    """Remove Telegram-specific expandable quote sentinels."""
    for s in (
        TranscriptParser.EXPANDABLE_QUOTE_START,
        TranscriptParser.EXPANDABLE_QUOTE_END,
    ):
        text = text.replace(s, "")
    return text


def _parse_table_cells(line: str) -> list[str]:
    """Extract cell values from a ``| a | b | c |`` line."""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _cell_block(text: str) -> dict[str, Any]:
    """Build a cell element — ``raw_text`` for plain text."""
    return {"type": "raw_text", "text": text}


def _table_to_block(table_lines: list[str]) -> dict[str, Any]:
    """Convert raw markdown table lines into a Block Kit ``table`` block."""
    headers: list[str] = []
    rows: list[list[str]] = []

    for line in table_lines:
        if _SEPARATOR_RE.match(line):
            continue
        cells = _parse_table_cells(line)
        if not headers:
            headers = cells
        else:
            rows.append(cells)

    col_count = len(headers)
    column_settings = [{"is_wrapped": True} for _ in range(col_count)]

    block_rows: list[list[dict[str, Any]]] = []
    block_rows.append([_cell_block(h) for h in headers])
    for row in rows:
        # Pad or trim to match header column count
        padded = row[:col_count] + [""] * max(0, col_count - len(row))
        block_rows.append([_cell_block(c) for c in padded])

    return {
        "type": "table",
        "column_settings": column_settings,
        "rows": block_rows,
    }


def _split_segments(text: str) -> list[str | list[str]]:
    """Split text into alternating text segments and table-line groups.

    Returns a list where each element is either a ``str`` (non-table text)
    or a ``list[str]`` (consecutive table lines). Segments inside fenced
    code blocks are never treated as tables.
    """
    lines = text.split("\n")
    segments: list[str | list[str]] = []
    text_buf: list[str] = []
    table_buf: list[str] = []
    in_code_block = False

    def _flush_text() -> None:
        if text_buf:
            segments.append("\n".join(text_buf))
            text_buf.clear()

    def _flush_table() -> None:
        # Require header + separator (2+ lines) to be a real table
        if len(table_buf) >= 2 and any(_SEPARATOR_RE.match(ln) for ln in table_buf):
            segments.append(list(table_buf))
        elif table_buf:
            text_buf.extend(table_buf)
        table_buf.clear()

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("```"):
            _flush_table()
            in_code_block = not in_code_block
            text_buf.append(line)
            continue

        if in_code_block:
            text_buf.append(line)
            continue

        if _TABLE_ROW_RE.match(line):
            _flush_text()
            table_buf.append(line)
        else:
            _flush_table()
            text_buf.append(line)

    _flush_table()
    _flush_text()
    return segments


def to_blocks(text: str) -> list[dict[str, Any]] | None:
    """Convert text to Block Kit blocks if it contains Markdown tables.

    Returns ``None`` when the text has no tables (caller should use plain mrkdwn).
    When tables are found, returns a list of blocks mixing ``section`` (for text)
    and ``table`` (for tables).
    """
    text = strip_sentinels(text)
    segments = _split_segments(text)

    has_table = any(isinstance(seg, list) for seg in segments)
    if not has_table:
        return None

    blocks: list[dict[str, Any]] = []
    for seg in segments:
        if isinstance(seg, list):
            blocks.append(_table_to_block(seg))
        else:
            mrkdwn_text = to_mrkdwn(seg)
            if mrkdwn_text.strip():
                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": mrkdwn_text},
                    }
                )
    return blocks


def to_mrkdwn(text: str) -> str:
    """Convert standard Markdown to Slack mrkdwn format."""
    text = strip_sentinels(text)

    # Protect code blocks and inline code with placeholders
    placeholders: list[str] = []

    def _save(replacement: str) -> str:
        idx = len(placeholders)
        placeholders.append(replacement)
        return f"{_PLACEHOLDER_PREFIX}{idx}\x00"

    def _save_code_block(m: re.Match[str]) -> str:
        content = m.group(1)
        # Ensure content ends with newline before closing ```
        if content and not content.endswith("\n"):
            content += "\n"
        return _save(f"```\n{content}```")

    def _save_inline_code(m: re.Match[str]) -> str:
        return _save(m.group(0))

    # Protect fenced code blocks first (with optional language tag)
    text = re.sub(r"```\w*\n(.*?)```", _save_code_block, text, flags=re.DOTALL)

    # Protect inline code
    text = re.sub(r"`[^`]+`", _save_inline_code, text)

    # Links: [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)

    # Bold: **text** → temporary markers (to avoid italic regex matching them)
    text = re.sub(r"\*\*(.+?)\*\*", rf"{_BOLD_OPEN}\1{_BOLD_CLOSE}", text)

    # Italic: *text* → _text_ (single asterisks only, bold already replaced)
    text = re.sub(r"\*(.+?)\*", r"_\1_", text)

    # Headings: ## Text → bold markers (applied after italic to avoid conflict)
    text = re.sub(
        r"^#{1,6}\s+(.+)$",
        rf"{_BOLD_OPEN}\1{_BOLD_CLOSE}",
        text,
        flags=re.MULTILINE,
    )

    # Resolve bold markers to *
    text = text.replace(_BOLD_OPEN, "*").replace(_BOLD_CLOSE, "*")

    # Strikethrough: ~~text~~ → ~text~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)

    # Restore placeholders
    for i in range(len(placeholders) - 1, -1, -1):
        text = text.replace(f"{_PLACEHOLDER_PREFIX}{i}\x00", placeholders[i])

    return text
