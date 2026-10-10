import json
import logging
from datetime import UTC, datetime

from .context import current_request_id


class StructuredFormatter(logging.Formatter):
    """Emit one JSON object per log line for ingestion by log platforms."""

    def format(self, record):
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = current_request_id()
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)
