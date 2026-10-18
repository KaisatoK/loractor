"""Scroll-to-bottom helper for the live investigation panel. The implementation
lives in :mod:`ui.autoscroll.autoscroll`; this module re-exports the public entry
point.
"""

from ui.autoscroll.autoscroll import auto_scroll

__all__ = ["auto_scroll"]
