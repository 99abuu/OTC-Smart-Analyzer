
import json
import logging
import threading
import uuid
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

HISTORY_FILE = Path(__file__).with_name("trade_history.jsonl")
_lock = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_event(event):
    """Append one event to the local history file."""
    try:
        line = json.dumps(
            event,
            ensure_ascii=False,
            allow_nan=False,
            default=str
        )

        with _lock:
            with HISTORY_FILE.open("a", encoding="utf-8") as file:
                file.write(line + "\n")
                file.flush()

        return True

    except Exception:
        logger.exception("Could not save trade history")
        return False


def record_signal(signal):
    """Save a complete snapshot of a generated signal."""
    signal_id = str(uuid.uuid4())

    event = {
        "event": "signal",
        "signal_id": signal_id,
        "timestamp": utc_now(),
        "signal": signal
    }

    if write_event(event):
        logger.info("Signal saved to history: %s", signal_id)
        return signal_id

    return None


def record_update(signal_id, status, details=None):
    """Save confirmation, cancellation, or trade-start events."""
    if not signal_id:
        return False

    event = {
        "event": "update",
        "signal_id": signal_id,
        "timestamp": utc_now(),
        "status": status,
        "details": details or {}
    }

    return write_event(event)


def record_result(
    signal_id,
    outcome,
    entry_price=None,
    exit_price=None,
    details=None
):
    """Save the final paper-trade result."""
    if not signal_id:
        return False

    event = {
        "event": "result",
        "signal_id": signal_id,
        "timestamp": utc_now(),
        "outcome": str(outcome).upper(),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "details": details or {}
    }

    if write_event(event):
        logger.info(
            "Trade result saved: %s = %s",
            signal_id,
            outcome
        )
        return True

    return False
