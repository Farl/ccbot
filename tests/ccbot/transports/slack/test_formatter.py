"""Tests for Slack mrkdwn formatter."""

from ccbot.transports.slack.formatter import to_mrkdwn


class TestBold:
    def test_double_asterisk_bold(self) -> None:
        assert to_mrkdwn("**bold**") == "*bold*"

    def test_bold_in_sentence(self) -> None:
        assert to_mrkdwn("this is **important** text") == "this is *important* text"

    def test_multiple_bold(self) -> None:
        assert to_mrkdwn("**a** and **b**") == "*a* and *b*"


class TestItalic:
    def test_single_asterisk_italic(self) -> None:
        assert to_mrkdwn("*italic*") == "_italic_"

    def test_italic_in_sentence(self) -> None:
        assert to_mrkdwn("some *emphasized* words") == "some _emphasized_ words"


class TestInlineCode:
    def test_inline_code_unchanged(self) -> None:
        assert to_mrkdwn("`code`") == "`code`"

    def test_inline_code_in_sentence(self) -> None:
        assert to_mrkdwn("run `pip install`") == "run `pip install`"


class TestCodeBlock:
    def test_code_block_strips_language(self) -> None:
        md = "```python\nprint('hello')\n```"
        expected = "```\nprint('hello')\n```"
        assert to_mrkdwn(md) == expected

    def test_code_block_no_language(self) -> None:
        md = "```\nsome code\n```"
        expected = "```\nsome code\n```"
        assert to_mrkdwn(md) == expected

    def test_code_block_preserves_content(self) -> None:
        md = "```js\nconst x = **not bold**;\n```"
        expected = "```\nconst x = **not bold**;\n```"
        assert to_mrkdwn(md) == expected


class TestLinks:
    def test_markdown_link(self) -> None:
        assert (
            to_mrkdwn("[click here](https://example.com)")
            == "<https://example.com|click here>"
        )

    def test_link_in_sentence(self) -> None:
        result = to_mrkdwn("see [docs](https://docs.io) for info")
        assert result == "see <https://docs.io|docs> for info"


class TestHeadings:
    def test_h1(self) -> None:
        assert to_mrkdwn("# Title") == "*Title*"

    def test_h2(self) -> None:
        assert to_mrkdwn("## Section") == "*Section*"

    def test_h3(self) -> None:
        assert to_mrkdwn("### Subsection") == "*Subsection*"


class TestStrikethrough:
    def test_strikethrough(self) -> None:
        assert to_mrkdwn("~~deleted~~") == "~deleted~"

    def test_strikethrough_in_sentence(self) -> None:
        assert to_mrkdwn("was ~~wrong~~ right") == "was ~wrong~ right"


class TestMixedContent:
    def test_bold_and_italic(self) -> None:
        assert to_mrkdwn("**bold** and *italic*") == "*bold* and _italic_"

    def test_bold_italic_code(self) -> None:
        result = to_mrkdwn("**bold** `code` *italic*")
        assert result == "*bold* `code` _italic_"

    def test_heading_with_link(self) -> None:
        result = to_mrkdwn("## See [docs](https://x.com)")
        assert result == "*See <https://x.com|docs>*"

    def test_code_block_not_transformed(self) -> None:
        md = "**bold** then\n```python\n**not bold**\n```\n*italic*"
        result = to_mrkdwn(md)
        assert "*bold*" in result
        assert "**not bold**" in result
        assert "_italic_" in result


class TestPlainText:
    def test_plain_passthrough(self) -> None:
        assert to_mrkdwn("hello world") == "hello world"

    def test_empty_string(self) -> None:
        assert to_mrkdwn("") == ""


class TestSentinelStripping:
    def test_strips_expandable_quote_sentinels(self) -> None:
        text = "\x02EXPQUOTE_START\x02some text\x02EXPQUOTE_END\x02"
        assert to_mrkdwn(text) == "some text"
