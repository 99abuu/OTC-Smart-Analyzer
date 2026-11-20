import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

HISTORY_FILE = Path(__file__).with_name("trade_history.jsonl")
HORIZONS = (60, 120)

MIN_TRAINING_SAMPLES = 40
MIN_CLASS_SAMPLES = 10

FEATURE_COUNT = 14
FEATURE_SCHEMA_VERSION = 1

TRAINING_EPOCHS = 400
LEARNING_RATE = 0.12
L2_REGULARIZATION = 0.03

VALIDATION_FRACTION = 0.20
MIN_VALIDATION_SAMPLES = 8
VALIDATION_MIN_TRAINING_SAMPLES = 25
VALIDATION_MIN_CLASS_SAMPLES = 6


def safe_number(value, default=0.0):
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError, OverflowError):
        pass
    return default


def clamp(value, low, high):
    return max(low, min(high, value))


def _parse_timestamp(value):
    """Return a UTC Unix timestamp, or None when the value is unusable."""
    if isinstance(value, (int, float)):
        number = safe_number(value, None)
        return number if number is not None and number > 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _strategy_names(value):
    if not isinstance(value, (list, tuple, set)):
        return []
    names = []
    for item in value:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("strategy") or item.get("id") or "").strip()
        else:
            name = str(item).strip() if item is not None else ""
        if name:
            names.append(name.casefold())
    return list(dict.fromkeys(names))


def extract_features(signal):
    if not isinstance(signal, dict):
        raise TypeError("signal must be a dictionary")

    price = abs(safe_number(signal.get("price")))
    atr = abs(safe_number(signal.get("atr", signal.get("ATR"))))
    ema20 = safe_number(signal.get("ema20", signal.get("EMA20")))
    ema50 = safe_number(signal.get("ema50", signal.get("EMA50")))
    ema200 = safe_number(signal.get("ema200", signal.get("EMA200")))
    rsi = safe_number(signal.get("rsi", signal.get("RSI")))
    adx = safe_number(signal.get("adx", signal.get("ADX")))
    di_plus = safe_number(signal.get("plus_di", signal.get("DI_plus")))
    di_minus = safe_number(signal.get("minus_di", signal.get("DI_minus")))
    body_ratio = safe_number(signal.get("body_ratio"))
    range_ratio = safe_number(signal.get("range_ratio"))
    close_position = safe_number(signal.get("close_position"))
    score = safe_number(signal.get("score"))

    direction = str(signal.get("direction", "")).upper().strip()
    trend = str(signal.get("trend", "")).upper().strip()
    matching = _strategy_names(signal.get("matching_strategies", []))

    trend_value = (
        1.0 if trend in ("UP", "BULLISH", "UPTREND", "BULL")
        else -1.0 if trend in ("DOWN", "BEARISH", "DOWNTREND", "BEAR")
        else 0.0
    )
    direction_value = 1.0 if direction == "CALL" else -1.0 if direction == "PUT" else 0.0

    atr_percent = atr / price * 100.0 if price > 0 else 0.0
    ema20_distance = (price - ema20) / price * 100.0 if price > 0 and ema20 else 0.0
    ema50_distance = (price - ema50) / price * 100.0 if price > 0 and ema50 else 0.0
    ema200_distance = (price - ema200) / price * 100.0 if price > 0 and ema200 else 0.0

    features = [
        clamp(score / 100.0, 0.0, 1.0),
        clamp(rsi / 100.0, 0.0, 1.0),
        clamp(adx / 100.0, 0.0, 1.0),
        clamp((di_plus - di_minus) / 100.0, -1.0, 1.0),
        clamp(atr_percent / 5.0, 0.0, 1.0),
        clamp(body_ratio, 0.0, 1.0),
        clamp(range_ratio / 3.0, 0.0, 1.0),
        clamp(close_position, 0.0, 1.0),
        clamp(ema20_distance / 2.0, -1.0, 1.0),
        clamp(ema50_distance / 2.0, -1.0, 1.0),
        clamp(ema200_distance / 2.0, -1.0, 1.0),
        trend_value,
        direction_value,
        clamp(len(matching) / 4.0, 0.0, 1.0),
    ]
    if len(features) != FEATURE_COUNT:
        raise ValueError(f"Expected {FEATURE_COUNT} features, got {len(features)}")
    return features


def read_training_records(history_file=None):
    """
    Return leakage-aware records:
    {features, target, signal_ts, label_ts, signal_id}.
    A label is only eligible for a prediction after its horizon_result timestamp.
    """
    path = Path(history_file) if history_file else HISTORY_FILE
    if not path.exists():
        return {60: [], 120: []}

    signals = {}
    labels = {60: {}, 120: {}}

    try:
        with path.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Skipping malformed history line %s", line_number)
                    continue
                if not isinstance(event, dict):
                    continue

                event_type = event.get("event")
                signal_id = event.get("signal_id")
                if not signal_id:
                    continue

                if event_type == "signal":
                    signal = event.get("signal")
                    signal_ts = _parse_timestamp(event.get("timestamp"))
                    if isinstance(signal, dict) and signal_ts is not None:
                        signals[signal_id] = (signal, signal_ts)

                elif event_type == "horizon_result":
                    try:
                        horizon = int(event.get("horizon_seconds"))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if horizon not in HORIZONS:
                        continue
                    outcome = str(event.get("outcome", "")).upper().strip()
                    label_ts = _parse_timestamp(event.get("timestamp"))
                    if outcome not in ("WIN", "LOSS") or label_ts is None:
                        continue
                    # Keep the first valid definitive label for each signal/horizon.
                    labels[horizon].setdefault(signal_id, (outcome, label_ts))
    except OSError:
        logger.exception("Could not read AI training history")
        return {60: [], 120: []}

    records = {60: [], 120: []}
    for horizon in HORIZONS:
        for signal_id, (outcome, label_ts) in labels[horizon].items():
            stored = signals.get(signal_id)
            if not stored:
                continue
            signal, signal_ts = stored
            try:
                features = extract_features(signal)
            except Exception:
                logger.exception("Could not extract AI features for signal %s", signal_id)
                continue
            records[horizon].append({
                "features": features,
                "target": 1.0 if outcome == "WIN" else 0.0,
                "signal_ts": signal_ts,
                "label_ts": label_ts,
                "signal_id": signal_id,
            })
        records[horizon].sort(key=lambda item: (item["signal_ts"], item["signal_id"]))
    return records


def read_training_data(history_file=None):
    """Backward-compatible tuple view used by older callers."""
    records = read_training_records(history_file)
    return {
        horizon: [(row["features"], row["target"]) for row in records[horizon]]
        for horizon in HORIZONS
    }


def _eligible_records(records, prediction_ts):
    """Only earlier signals whose outcome was already known at prediction time."""
    return [
        row for row in records
        if row["signal_ts"] < prediction_ts and row["label_ts"] < prediction_ts
    ]


def sigmoid(value):
    value = clamp(value, -30.0, 30.0)
    return 1.0 / (1.0 + math.exp(-value))


def predict_probability(weights, features):
    if len(weights) != FEATURE_COUNT + 1:
        raise ValueError("Model weight count does not match feature schema")
    if len(features) != FEATURE_COUNT:
        raise ValueError("Feature count does not match model schema")
    value = weights[0]
    for index, feature in enumerate(features):
        value += weights[index + 1] * feature
    return sigmoid(value)


def _class_counts(dataset):
    wins = sum(1 for _, target in dataset if target == 1.0)
    losses = sum(1 for _, target in dataset if target == 0.0)
    return wins, losses


def _fit_logistic(dataset, min_samples, min_class_samples):
    total = len(dataset)
    wins, losses = _class_counts(dataset)
    if total < min_samples:
        return None, {"status": "INSUFFICIENT_DATA", "samples": total, "wins": wins, "losses": losses}
    if wins < min_class_samples or losses < min_class_samples:
        return None, {"status": "CLASS_IMBALANCE", "samples": total, "wins": wins, "losses": losses}

    weights = [0.0] * (FEATURE_COUNT + 1)
    for _ in range(TRAINING_EPOCHS):
        gradients = [0.0] * len(weights)
        for features, target in dataset:
            prediction = predict_probability(weights, features)
            error = prediction - target
            gradients[0] += error
            for index, feature in enumerate(features):
                gradients[index + 1] += error * feature
        for index in range(len(weights)):
            gradient = gradients[index] / total
            if index > 0:
                gradient += L2_REGULARIZATION * weights[index]
            weights[index] -= LEARNING_RATE * gradient

    return weights, {"status": "READY", "samples": total, "wins": wins, "losses": losses}


def _validation_metrics(weights, dataset):
    total = len(dataset)
    if not total:
        return {"samples": 0, "accuracy": None, "brier_score": None, "log_loss": None}
    correct = 0
    brier_total = 0.0
    log_loss_total = 0.0
    epsilon = 1e-9
    for features, target in dataset:
        probability = predict_probability(weights, features)
        if (1.0 if probability >= 0.5 else 0.0) == target:
            correct += 1
        brier_total += (probability - target) ** 2
        safe_probability = clamp(probability, epsilon, 1.0 - epsilon)
        log_loss_total += -(target * math.log(safe_probability) + (1.0 - target) * math.log(1.0 - safe_probability))
    return {
        "samples": total,
        "accuracy": round(correct / total * 100.0, 2),
        "brier_score": round(brier_total / total, 5),
        "log_loss": round(log_loss_total / total, 5),
    }


def _temporal_validation_records(records):
    total = len(records)
    validation_count = max(MIN_VALIDATION_SAMPLES, int(total * VALIDATION_FRACTION))
    split_index = total - validation_count
    if split_index <= 0 or validation_count < MIN_VALIDATION_SAMPLES:
        return {
            "status": "NOT_ENOUGH_DATA", "samples": 0, "accuracy": None,
            "brier_score": None, "log_loss": None,
            "training_samples_before_validation": max(0, split_index),
        }

    validation_rows = records[split_index:]
    validation_start = validation_rows[0]["signal_ts"]

    # Strict holdout: every training label must have been known before
    # the first validation signal appeared.
    training_rows = [
        row for row in records[:split_index]
        if row["signal_ts"] < validation_start and row["label_ts"] < validation_start
    ]
    training = [(row["features"], row["target"]) for row in training_rows]

    weights, info = _fit_logistic(
        training,
        VALIDATION_MIN_TRAINING_SAMPLES,
        VALIDATION_MIN_CLASS_SAMPLES,
    )
    if weights is None:
        return {
            "status": info["status"], "samples": 0, "accuracy": None,
            "brier_score": None, "log_loss": None,
            "training_samples_before_validation": len(training),
            "validation_candidates": len(validation_rows),
        }

    validation = [(row["features"], row["target"]) for row in validation_rows]
    metrics = _validation_metrics(weights, validation)
    metrics.update({
        "status": "READY",
        "training_samples_before_validation": len(training),
        "validation_candidates": len(validation_rows),
        "validation_start": datetime.fromtimestamp(validation_start, timezone.utc).isoformat(),
    })
    return metrics


def _temporal_validation(dataset):
    """Compatibility path for legacy tuple datasets without timestamps."""
    total = len(dataset)
    validation_count = max(MIN_VALIDATION_SAMPLES, int(total * VALIDATION_FRACTION))
    split_index = total - validation_count
    if split_index < VALIDATION_MIN_TRAINING_SAMPLES:
        return {
            "status": "NOT_ENOUGH_DATA", "samples": 0, "accuracy": None,
            "brier_score": None, "log_loss": None,
            "training_samples_before_validation": max(0, split_index),
        }
    training = dataset[:split_index]
    validation = dataset[split_index:]
    weights, info = _fit_logistic(training, VALIDATION_MIN_TRAINING_SAMPLES, VALIDATION_MIN_CLASS_SAMPLES)
    if weights is None:
        return {
            "status": info["status"], "samples": 0, "accuracy": None,
            "brier_score": None, "log_loss": None,
            "training_samples_before_validation": len(training),
        }
    metrics = _validation_metrics(weights, validation)
    metrics.update({"status": "READY", "training_samples_before_validation": len(training)})
    return metrics


def train_model(dataset):
    """Train on all supplied eligible examples; validate with strict time holdout when metadata exists."""
    if dataset and isinstance(dataset[0], dict) and "signal_ts" in dataset[0]:
        validation = _temporal_validation_records(dataset)
        usable = [(row["features"], row["target"]) for row in dataset]
    else:
        validation = _temporal_validation(dataset)
        usable = dataset

    weights, info = _fit_logistic(usable, MIN_TRAINING_SAMPLES, MIN_CLASS_SAMPLES)
    info["validation"] = validation
    return weights, info


def evaluate_signal(signal, history_file=None):
    result = {
        "mode": "SHADOW",
        "model": "LogisticRegression",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "predictions": {},
    }
    try:
        features = extract_features(signal)
        records_by_horizon = read_training_records(history_file)
        prediction_ts = _parse_timestamp(signal.get("timestamp")) or time.time()

        for horizon in HORIZONS:
            eligible = _eligible_records(records_by_horizon[horizon], prediction_ts)
            dataset = [(row["features"], row["target"]) for row in eligible]
            weights, info = _fit_logistic(dataset, MIN_TRAINING_SAMPLES, MIN_CLASS_SAMPLES)
            prediction = {
                "status": info["status"],
                "samples": info["samples"],
                "wins": info["wins"],
                "losses": info["losses"],
                "win_probability": None,
                "validation": _temporal_validation_records(records_by_horizon[horizon]),
            }
            if weights is not None:
                prediction["win_probability"] = round(predict_probability(weights, features) * 100.0, 2)
            result["predictions"][str(horizon)] = prediction
    except Exception:
        logger.exception("AI shadow evaluation failed")
        result["error"] = "EVALUATION_FAILED"
    return result
