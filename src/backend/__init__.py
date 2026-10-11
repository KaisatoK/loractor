import logging
import os

from dotenv import load_dotenv

load_dotenv()


def _log_level() -> int:
    """Read LOG_LEVEL from the environment, defaulting to INFO on any bad value."""
    level = logging.getLevelName(os.environ.get("LOG_LEVEL", "INFO").upper())
    return level if isinstance(level, int) else logging.INFO


logging.basicConfig(
    level=_log_level(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
