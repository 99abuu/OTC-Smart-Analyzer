import logging
import math
import time
from datetime import datetime, timezone

from ai_model import (
    HORIZONS,
    MIN_CLASS_SAMPLES,
    MIN_TRAINING_SAMPLES,
    _eligible_records,
    _parse_timestamp,
    _temporal_validation_records,
    extract_features,
    read_training_records,
)

logger = logging.getLogger(__name__)

MODEL_NAME = "KNN_Shadow_v2"
MODEL_VERSION = 2

VALIDATION_FRACTION = 0.20
MIN_VALIDATION_SAMPLES = 8

MIN_NEIGHBORS = 5
MAX_NEIGHBORS = 15
DISTANCE_EPSILON = 0.05
FEATURE_COUNT = 14
STANDARD_DEVIATION_FLOOR = 0.05
STANDARDIZED_FEATURE_CLIP = 5.0


def _class_counts(dataset):
    wins = sum(1 for _, target in dataset if target == 1.0)
    losses = sum(1 for _, target in dataset if target == 0.0)
    return wins, losses


def _data_status(dataset):
    total = len(dataset)
    wins, losses = _class_counts(dataset)
    if total < MIN_TRAINING_SAMPLES:
        status = "INSUFFICIENT_DATA"
    elif wins < MIN_CLASS_SAMPLES or losses < MIN_CLASS_SAMPLES:
        status = "CLASS_IMBALANCE"
    else:
        status = "READY"
    return {"status": status, "samples": total, "wins": wins, "losses": losses}


def _fit_scaler(dataset):
    """Fit z-score scaling on training rows only; never inspect validation rows."""
    if not dataset:
        return None
    means = []
    scales = []
    for feature_index in range(FEATURE_COUNT):
        values = [row[0][feature_index] for row in dataset]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        std = math.sqrt(variance)
        means.append(mean)
        scales.append(max(std, STANDARD_DEVIATION_FLOOR))
    return {"means": means, "scales": scales}


def _transform(features, scaler):
    if len(features) != FEATURE_COUNT:
        raise ValueError("Feature count does not match schema")
    if scaler is None:
        raise ValueError("Scaler is unavailable")
    transformed = []
    for index, value in enumerate(features):
        z = (value - scaler["means"][index]) / scaler["scales"][index]
        transformed.append(max(-STANDARDIZED_FEATURE_CLIP, min(STANDARDIZED_FEATURE_CLIP, z)))
    return transformed


def _distance(first, second):
    if len(first) != FEATURE_COUNT or len(second) != FEATURE_COUNT:
        raise ValueError("Feature count does not match schema")
    squared_total = sum((left - right) ** 2 for left, right in zip(first, second))
    return math.sqrt(squared_total / FEATURE_COUNT)


def _neighbor_count(training_size):
    count = int(math.sqrt(training_size))
    count = max(MIN_NEIGHBORS, count)
    count = min(MAX_NEIGHBORS, count, training_size)
    if count > MIN_NEIGHBORS and count % 2 == 0:
        count -= 1
    return max(1, count)


def predict_probability(dataset, features, scaler=None):
    """
    KNN prediction. Dataset contains (features, target) pairs.
    Scaling parameters are fitted on the supplied training set only.
    """
    status = _data_status(dataset)
    if status["status"] != "READY":
        return None
    if len(features) != FEATURE_COUNT:
        raise ValueError("Feature count does not match schema")

    fitted_scaler = scaler or _fit_scaler(dataset)
    query = _transform(features, fitted_scaler)
    ranked = []
    for historical_features, target in dataset:
        point = _transform(historical_features, fitted_scaler)
        ranked.append((_distance(query, point), target))
    ranked.sort(key=lambda item: item[0])

    neighbor_count = _neighbor_count(len(ranked))
    nearest = ranked[:neighbor_count]
    weighted_wins = 0.0
    total_weight = 0.0
    for distance, target in nearest:
        weight = 1.0 / (distance + DISTANCE_EPSILON)
        weighted_wins += weight * target
        total_weight += weight

    if total_weight <= 0:
        return None
    probability = (weighted_wins + 1.0) / (total_weight + 2.0)
    return {
        "probability": max(0.0, min(1.0, probability)),
        "neighbors_used": neighbor_count,
    }


def _calculate_metrics(predictions):
    total = len(predictions)
    if total == 0:
        return {"samples": 0, "accuracy": None, "brier_score": None, "log_loss": None}
    correct = 0
    brier_total = 0.0
    log_loss_total = 0.0
    epsilon = 1e-9
    for probability, target in predictions:
        if (1.0 if probability >= 0.5 else 0.0) == target:
            correct += 1
        brier_total += (probability - target) ** 2
        safe_probability = max(epsilon, min(1.0 - epsilon, probability))
        log_loss_total += -(target * math.log(safe_probability) + (1.0 - target) * math.log(1.0 - safe_probability))
    return {
        "samples": total,
        "accuracy": round(correct / total * 100.0, 2),
        "brier_score": round(brier_total / total, 5),
        "log_loss": round(log_loss_total / total, 5),
    }


def temporal_validation(records):
    """
    Strict chronological holdout. Validation labels are never included in
    scaler fitting or neighbor selection. Training labels must be available
    before the first validation signal timestamp.
    """
    total = len(records)
    validation_count = max(MIN_VALIDATION_SAMPLES, int(total * VALIDATION_FRACTION))
    split_index = total - validation_count
    if split_index <= 0 or validation_count < MIN_VALIDATION_SAMPLES:
        return {
            "status": "NOT_ENOUGH_DATA", "samples": 0, "accuracy": None,
            "brier_score": None, "log_loss": None,
            "training_samples_before_validation": max(0, split_index),
            "validation_candidates": max(0, validation_count),
        }

    validation_rows = records[split_index:]
    validation_start = validation_rows[0]["signal_ts"]
    training_rows = [
        row for row in records[:split_index]
        if row["signal_ts"] < validation_start and row["label_ts"] < validation_start
    ]
    training = [(row["features"], row["target"]) for row in training_rows]
    status = _data_status(training)
    if status["status"] != "READY":
        return {
            "status": status["status"], "samples": 0, "accuracy": None,
            "brier_score": None, "log_loss": None,
            "training_samples_before_validation": len(training),
            "validation_candidates": len(validation_rows),
        }

    scaler = _fit_scaler(training)
    predictions = []
    skipped = 0
    for row in validation_rows:
        estimate = predict_probability(training, row["features"], scaler=scaler)
        if estimate is None:
            skipped += 1
            continue
        predictions.append((estimate["probability"], row["target"]))

    metrics = _calculate_metrics(predictions)
    metrics.update({
        "status": "READY" if len(predictions) >= MIN_VALIDATION_SAMPLES else "NOT_ENOUGH_VALIDATION_DATA",
        "training_samples_before_validation": len(training),
        "validation_candidates": len(validation_rows),
        "skipped": skipped,
        "validation_start": datetime.fromtimestamp(validation_start, timezone.utc).isoformat(),
    })
    return metrics


def train_model(dataset):
    """Compatibility helper for tuple datasets; timestamped records are preferred."""
    if dataset and isinstance(dataset[0], dict) and "signal_ts" in dataset[0]:
        records = dataset
        validation = temporal_validation(records)
        usable = [(row["features"], row["target"]) for row in records]
    else:
        usable = dataset
        validation = {"status": "LEGACY_DATASET_NO_TIMESTAMPS", "samples": 0,
                      "accuracy": None, "brier_score": None, "log_loss": None,
                      "training_samples_before_validation": 0}
    info = _data_status(usable)
    info["validation"] = validation
    info["model"] = MODEL_NAME
    info["version"] = MODEL_VERSION
    return info


def evaluate_signal(signal, history_file=None):
    """Independent KNN assessment in Shadow Mode; never blocks baseline signals."""
    result = {
        "mode": "SHADOW",
        "model": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "feature_schema_version": 1,
        "filter_status": "OBSERVE_ONLY",
        "predictions": {},
    }
    try:
        features = extract_features(signal)
        records_by_horizon = read_training_records(history_file)
        prediction_ts = _parse_timestamp(signal.get("timestamp")) or time.time()

        for horizon in HORIZONS:
            records = records_by_horizon[horizon]
            eligible = _eligible_records(records, prediction_ts)
            dataset = [(row["features"], row["target"]) for row in eligible]
            info = _data_status(dataset)
            validation = temporal_validation(records)

            prediction = {
                "status": info["status"],
                "samples": info["samples"],
                "wins": info["wins"],
                "losses": info["losses"],
                "win_probability": None,
                "neighbors_used": None,
                "validation": validation,
            }

            if info["status"] == "READY":
                scaler = _fit_scaler(dataset)
                estimate = predict_probability(dataset, features, scaler=scaler)
                if estimate is not None:
                    prediction["win_probability"] = round(estimate["probability"] * 100.0, 2)
                    prediction["neighbors_used"] = estimate["neighbors_used"]

            result["predictions"][str(horizon)] = prediction
    except Exception:
        logger.exception("KNN shadow evaluation failed")
        result["error"] = "EVALUATION_FAILED"
    return result
