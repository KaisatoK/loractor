"""How a chat turn is shown to the pipeline's agents and classifiers."""

# Appended to a user turn whose investigation the user stopped, so the agents can
# tell "asked but never answered" from an ordinary unanswered message.
STOPPED_NOTE = "[investigation stopped by the user; no answer was given]"


def label_stopped(text: str, stopped: bool) -> str:
    """``text`` with the stopped note appended when ``stopped`` is set."""
    return f"{text} {STOPPED_NOTE}" if stopped else text
