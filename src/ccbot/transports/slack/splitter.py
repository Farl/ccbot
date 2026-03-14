"""Message splitting for Slack's 4000-character limit."""

SLACK_MAX_LENGTH = 4000


def split_message(text: str, max_length: int = SLACK_MAX_LENGTH) -> list[str]:
    """Split text into chunks that fit within Slack's message limit.

    Tries to split at newlines. Falls back to hard split if a single line
    exceeds max_length.
    """
    if len(text) <= max_length:
        return [text]

    parts: list[str] = []
    current = ""

    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= max_length:
            current = candidate
        else:
            if current:
                parts.append(current)
            if len(line) > max_length:
                for i in range(0, len(line), max_length):
                    parts.append(line[i : i + max_length])
                current = ""
            else:
                current = line

    if current:
        parts.append(current)

    return parts
