"""Scroll-to-bottom helper for the live investigation panel.

Rendered inside the run's polling fragment: every rerun reposts the component's
render message, and the frontend smoothly follows the bottom, so the streaming
thinking and answer text stays in view. The follow releases when the reader
scrolls up and re-engages once they return to the bottom.
"""

from pathlib import Path

import streamlit.components.v1 as components

_DIR = Path(__file__).parent
_component = components.declare_component("auto_scroll", path=str(_DIR))

# A stable key keeps the component's iframe mounted as its arguments change, so
# the frontend's scroll state survives each update. Without it the iframe is
# remounted (its element identity is derived from the arguments), the script
# re-runs with its state reset, and the follow fights the reader on every update.
_KEY = "auto_scroll"


def auto_scroll(content_len: int, run_id: str) -> None:
    """Ask the app to keep the streaming text in view.

    ``content_len`` is the number of characters of thinking/answer text rendered
    so far, used only as a change signal: it grows whenever new text is painted
    (a new progress line, a longer thought, a longer answer) and the frontend is
    reposted, so it follows the bottom smoothly; an unchanged value means
    nothing new and nothing scrolls. The reader scrolling up releases the follow
    until they return to the bottom. ``run_id`` scopes that release to one
    investigation, so a new run starts following again. Rendered at zero height,
    it occupies no space in the page.
    """
    _component(content_len=content_len, run_id=run_id, height=0, key=_KEY)
