"""JSON log records for the worker and the parser sidecar.

Kept apart from consumer.py so the parser sidecar can use it without importing
boto3, which consumer.py does at module scope.
"""

import json
import logging
import os


class JsonFormatter(logging.Formatter):
    """Render each record as one JSON object, built with json.dumps.

    The worker used to log through a format string that only looked like JSON:
    ``"message":"%(message)s"`` with the message pasted in unescaped. The message
    carries the job's input key, which a holder chooses, so a quote in it forged
    fields and a line feed forged whole records in the logs responders rely on
    (T59). json.dumps escapes both, and with ensure_ascii every other control or
    line-separator character too, so one call to the logger is always one line.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "module": record.module,
            "message": record.getMessage(),
        }
        if record.exc_info:
            # Tracebacks span lines; inside the object they are one escaped string.
            entry["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(entry, default=str)


def configure_json_logging() -> None:
    """Send the root logger's records to stderr as JSON, at LOG_LEVEL."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        handlers=[handler],
    )
