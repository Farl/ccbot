"""Silent-mode classification.

Silent mode is applied per bound window at DELIVERY (handle_new_message), not in
the monitor. The monitor emits every category (subject only to global config);
delivery drops the "noise" for silenced windows and keeps the assistant's
replies. NewMessage.is_noise is the shared predicate for that decision.
"""

import pytest

from ccbot.session_monitor import NewMessage


def _msg(role="assistant", content_type="text") -> NewMessage:
    return NewMessage(
        session_id="s", text="x", is_complete=True, role=role, content_type=content_type
    )


@pytest.mark.parametrize(
    ("role", "content_type", "expected"),
    [
        ("user", "text", True),  # user echo
        ("assistant", "thinking", True),
        ("assistant", "tool_use", True),
        ("assistant", "tool_result", True),
        ("assistant", "local_command", True),  # slash-cmd echo (❯ /clear)
        ("assistant", "text", False),  # the reply — always kept
    ],
)
def test_is_noise(role, content_type, expected):
    assert _msg(role=role, content_type=content_type).is_noise is expected
