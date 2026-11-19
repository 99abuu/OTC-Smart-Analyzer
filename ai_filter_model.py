
import logging
import math

from ai_model import (
    HORIZONS,
    extract_features,
    read_training_data,
)

logger = logging.getLogger(__name__)

MODEL_NAME = "KNN_Shadow_v1"
MODEL_VERSION = 1

MIN_TRAINING_SAMPLES = 40
MIN_CLASS_SAMPLES = 10

VALIDATION_FRACTION = 0.20
MIN_VALIDATION_SAMPLES = 8

MIN_NEIGHBORS = 5
MAX_NEIGHBORS = 15
DISTANCE_EPSILON = 0.05

FEATURE_COUNT = 14


def _class_counts(dataset):
    wins = sum(1 for _, target in dataset if target == 1.0)
    losses = sum(1 for _, target in dataset if target == 0.0)
    return wins, losses


def _data_status(dataset, min_samples=MIN_TRAINING_SAMPLES):
    total = len(dataset)
    wins, losses = _class_counts(dataset)

    if total < min_samples:
        status = "INSUFFICIENT_DATA"
    elif wins < MIN_CLASS_SAMPLES or losses < MIN_CLASS_SAMPLES:
        status = "CLASS_IMBALANCE"
    else:
        status = "READY"

    return {
        "status": status,
        "samples": total,
        "wins": wins,
        "losses": losses,
    }


def _distance(first, second):
    if len(first) != FEATURE_COUNT or len(second) != FEATURE_COUNT:
        raise ValueError("Feature count does not match schema")

    squared_total = 0.0

    for left, right in zip(first, second):
        difference = left - right
        squared_total += difference * difference

    return math.sqrt(squared_total / FEATURE_COUNT)


def _neighbor_count(training_size):
    count = int(math.sqrt(training_size))

    count = max(MIN_NEIGHBORS, count)
    count = min(MAX_NEIGHBORS, count, training_size)

    # Prefer an odd number when possible.
    if count > MIN_NEIGHBORS and count % 2 == 0:
        count -= 1

    return max(1, count)


def predict_probability(dataset, features):
    """
    KNN prediction using only the supplied historical dataset.

    Each historical example is ranked by Euclidean distance.
    Closer examples receive greater weight.

    Returns a probability between 0 and 1, or None if data
    is insufficient.
    """
    status = _data_status(dataset)

    if status["status"] != "READY":
        return None

    if len(features) != FEATURE_COUNT:
        raise ValueError("Feature count does not match schema")

    ranked = []

    for historical_features, target in dataset:
        distance = _distance(features, historical_features)
        ranked.append((distance, target))

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

    # Light Laplace smoothing avoids extreme 0% or 100% estimates
    # when the nearest examples happen to share the same outcome.
    probability = (weighted_wins + 1.0) / (total_weight + 2.0)

    return {
        "probability": max(0.0, min(1.0, probability)),
        "neighbors_used": neighbor_count,
    }


def _calculate_metrics(predictions):
    total = len(predictions)

    if total == 0:
        return {
            "samples": 0,
            "accuracy": None,
            "brier_score": None,
            "log_loss": None,
        }

    correct = 0
    brier_total = 0.0
    log_loss_total = 0.0
    epsilon = 1e-9

    for probability, target in predictions:
        predicted_class = 1.0 if probability >= 0.5 else 0.0

        if predicted_class == target:
            correct += 1

        brier_total += (probability - target) ** 2

        safe_probability = max(
            epsilon,
            min(1.0 - epsilon, probability),
        )

        log_loss_total += -(
            target * math.log(safe_probability)
            + (1.0 - target) * math.log(1.0 - safe_probability)
        )

    return {
        "samples": total,
        "accuracy": round(correct / total * 100.0, 2),
        "brier_score": round(brier_total / total, 5),
        "log_loss": round(log_loss_total / total, 5),
    }


def temporal_validation(dataset):
    """
    Walk-forward validation.

    The model predicts each validation example using only examples
    that occurred before it. It never trains on later validation rows.
    """
    total = len(dataset)

    validation_count = max(
        MIN_VALIDATION_SAMPLES,
        int(total * VALIDATION_FRACTION),
    )

    split_index = total - validation_count

    if split_index < MIN_TRAINING_SAMPLES:
        return {
            "status": "NOT_ENOUGH_DATA",
            "samples": 0,
            "accuracy": None,
            "brier_score": None,
            "log_loss": None,
            "training_samples_before_validation": split_index,
        }

    predictions = []
    skipped_insufficient = 0

    for index in range(split_index, total):
        # Only earlier observations are allowed for this prediction.
        historical_data = dataset[:index]
        features, target = dataset[index]

        status = _data_status(historical_data)

        if status["status"] != "READY":
            skipped_insufficient += 1
            continue

        prediction = predict_probability(historical_data, features)

        if prediction is None:
            skipped_insufficient += 1
            continue

        predictions.append((prediction["probability"], target))

    metrics = _calculate_metrics(predictions)

    if len(predictions) < MIN_VALIDATION_SAMPLES:
        metrics["status"] = "NOT_ENOUGH_VALIDATION_DATA"
    else:
        metrics["status"] = "READY"

    metrics["validation_candidates"] = validation_count
    metrics["skipped_insufficient"] = skipped_insufficient
    metrics["training_samples_before_validation"] = split_index

    return metrics


def train_model(dataset):
    """
    KNN does not learn a fixed weight vector.
    Its training set is the historical examples themselves.
    """
    info = _data_status(dataset)
    info["validation"] = temporal_validation(dataset)
    info["model"] = MODEL_NAME
    info["version"] = MODEL_VERSION

    return info


def evaluate_signal(signal, history_file=None):
    """
    Independent KNN assessment in Shadow Mode.

    This function does not change the original signal, its direction,
    strategy, duration, or Telegram delivery.

    No ACCEPT/REJECT threshold is applied in this version.
    """
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
        datasets = read_training_data(history_file)

        for horizon in HORIZONS:
            dataset = datasets.get(horizon, [])
            info = train_model(dataset)

            prediction = {
                "status": info["status"],
                "samples": info["samples"],
                "wins": info["wins"],
                "losses": info["losses"],
                "win_probability": None,
                "neighbors_used": None,
                "validation": info["validation"],
            }

            if info["status"] == "READY":
                estimate = predict_probability(dataset, features)

                if estimate is not None:
                    prediction["win_probability"] = round(
                        estimate["probability"] * 100.0,
                        2,
                    )
                    prediction["neighbors_used"] = estimate[
                        "neighbors_used"
                    ]

            result["predictions"][str(horizon)] = prediction

    except Exception:
        logger.exception("KNN shadow evaluation failed")
        result["error"] = "EVALUATION_FAILED"

    return result
