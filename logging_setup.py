import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(level: str) -> None:
    Path("logs").mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    if root.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file_handler = RotatingFileHandler(
        "logs/9n8.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(file_handler)
