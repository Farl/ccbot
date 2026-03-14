"""Slack mrkdwn formatter — converts standard Markdown to Slack's mrkdwn format.

Handles bold, italic, code (inline and block), links, headings, and strikethrough.
Code blocks and inline code are protected from transformation via placeholders.
"""

import re

from ...transcript_parser import TranscriptParser

_PLACEHOLDER_PREFIX = "\x00PH"
_BOLD_OPEN = "\x01BOLD_O\x01"
_BOLD_CLOSE = "\x01BOLD_C\x01"


def strip_sentinels(text: str) -> str:
    """Remove Telegram-specific expandable quote sentinels."""
    for s in (
        TranscriptParser.EXPANDABLE_QUOTE_START,
        TranscriptParser.EXPANDABLE_QUOTE_END,
    ):
        text = text.replace(s, "")
    return text


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
