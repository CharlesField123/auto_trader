"""Structured console logging.

Railway captures stdout/stderr, so a plain configured ``logging`` logger gives
us timestamped, level-tagged lines in the Railway log viewer with no extra
infrastructure.
"""

import logging
import sys


def get_logger(name: str = "auto-trader") -> logging.Logger:
    """Return a singleton-ish configured logger.

    Repeated calls reuse the same handler set so we never duplicate log lines.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger


log = get_logger()
