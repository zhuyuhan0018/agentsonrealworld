"""
Logging configuration for the agent sandbox.

Provides structured logging with file output and configurable console output levels.
"""

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Literal

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def setup_logging(
    log_dir: str = "logs",
    console_level: LogLevel = "INFO",
    file_level: LogLevel = "DEBUG",
    log_file: str = "agent_sandbox.log",
    max_bytes: int = 10 * 1024 * 1024,  # 10MB
    backup_count: int = 5
) -> logging.Logger:
    """
    Set up logging configuration.
    
    Configures the root logger so all child loggers inherit handlers.
    This ensures that loggers created with get_logger(__name__) will work correctly.
    
    Args:
        log_dir: Directory to store log files
        console_level: Minimum log level for console output (INFO, WARNING, ERROR, etc.)
        file_level: Minimum log level for file output (usually DEBUG to capture everything)
        log_file: Name of the log file
        max_bytes: Maximum size of log file before rotation
        backup_count: Number of backup log files to keep
    
    Returns:
        Configured logger instance named "agent_sandbox"
    """
    # Create logs directory if it doesn't exist
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    
    # Get root logger - configure the root logger so all child loggers inherit
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # Set to lowest level, handlers will filter
    
    # Prevent duplicate logs if called multiple times
    if root_logger.handlers:
        return logging.getLogger("agent_sandbox")
    
    # Create formatters
    detailed_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    simple_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )
    
    # File handler with rotation
    file_handler = logging.handlers.RotatingFileHandler(
        log_path / log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8'
    )
    file_handler.setLevel(getattr(logging, file_level))
    file_handler.setFormatter(detailed_formatter)
    root_logger.addHandler(file_handler)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, console_level))
    console_handler.setFormatter(simple_formatter)
    root_logger.addHandler(console_handler)
    
    # Ensure propagation is enabled (default, but explicit is better)
    root_logger.propagate = True
    
    # Disable verbose HTTP request logging from httpx and other HTTP libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.client").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.connector").setLevel(logging.WARNING)
    # Suppress Stainless SDK verbose logging (used by OpenAI/Qwen SDKs)
    logging.getLogger("stainless").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("openai._client").setLevel(logging.WARNING)
    logging.getLogger("openai._base_client").setLevel(logging.WARNING)
    logging.getLogger("openai._client._request").setLevel(logging.WARNING)
    # Suppress langchain OpenAI client verbose logging
    logging.getLogger("langchain_openai").setLevel(logging.WARNING)
    logging.getLogger("langchain_openai.chat_models").setLevel(logging.WARNING)
    
    # Get the main logger for return value
    logger = logging.getLogger("agent_sandbox")
    logger.info(f"Logging initialized - Console: {console_level}, File: {file_level}")
    logger.debug(f"Log file: {log_path / log_file}")
    
    return logger


def get_logger(name: str = "agent_sandbox") -> logging.Logger:
    """
    Get a logger instance by name.
    
    This function creates loggers that inherit from the root logger.
    All loggers created this way will use the handlers configured by setup_logging().
    
    Args:
        name: Logger name (typically __name__ from the calling module)
    
    Returns:
        Logger instance that inherits handlers from root logger
    """
    logger = logging.getLogger(name)
    # Ensure the logger propagates to root (default behavior, but explicit)
    logger.propagate = True
    return logger

