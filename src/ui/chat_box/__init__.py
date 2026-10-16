"""Type-ahead chat box, a no-build Streamlit custom component.
The implementation lives in :mod:`ui.chat_box.chat_box`; this module re-exports
the public entry point.
"""

from ui.chat_box.chat_box import chat_box

__all__ = ["chat_box"]
