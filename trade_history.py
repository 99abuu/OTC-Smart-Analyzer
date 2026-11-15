
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
    signal_id = str(uuid.uuid4())

    event = {
        "event": "signal",
        "schema_version": 2,
        "signal_id": signal_id,
        "timestamp": utc_now(),
        "signal": signal
    }

    if write_event(event):
        logger.info("Signal saved to history: %s", signal_id)
        return signal_id

    return None


def record_update(signal_id, status, details=None):
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


def record_horizon_result(
    signal_id,
    horizon_seconds,
    outcome,
    entry_price=None,
    exit_price=None,
    details=None
):
    if not signal_id:
        return False

    if horizon_seconds not in (60, 120):
        logger.error(
            "Unsupported result horizon: %s",
            horizon_seconds
        )
        return False

    normalized_outcome = str(outcome).upper()

    if normalized_outcome not in ("WIN", "LOSS", "DRAW"):
        logger.error(
            "Invalid horizon outcome: %s",
            normalized_outcome
        )
        return False

    event = {
        "event": "horizon_result",
        "schema_version": 1,
        "signal_id": signal_id,
        "timestamp": utc_now(),
        "horizon_seconds": horizon_seconds,
        "outcome": normalized_outcome,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "details": details or {}
    }

    if write_event(event):
        logger.info(
            "Horizon result saved: signal=%s duration=%ss outcome=%s",
            signal_id,
            horizon_seconds,
            normalized_outcome
        )
        return True

    return False
