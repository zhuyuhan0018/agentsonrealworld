"""Logging compatibility module for container code.

Container code uses standard Python logging module directly.
This module provides a simple get_logger function compatible with the host's logging_config.
"""

import logging


def get_logger(name: str) -> logging.Logger:
    """Get a logger using standard logging module.
    
    Container code doesn't need the host's logging_config setup.
    Uses standard Python logging which is sufficient for container runtime.
    """
    return logging.getLogger(name)

