
import json
import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

HISTORY_FILE = Path(__file__).with_name("trade_history.jsonl")

HORIZONS = (60, 120)

# لا نصدر توقعاً قبل توفر عينة تدريب معقولة
MIN_TRAINING_SAMPLES = 40
MIN_CLASS_SAMPLES = 10

FEATURE_COUNT = 14
TRAINING_EPOCHS = 300
LEARNING_RATE = 0.15
L2_REGULARIZATION = 0.02


def safe_number(value, default=0.0):
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError):
        pass

    return default


def clamp(value, low, high):
    return max(low, min(high, value))


def extract_features(signal):
    """استخراج خصائص الإشارة فقط، دون استخدام أي بيانات مستقبلية."""

    price = abs(safe_number(signal.get("price")))
    atr = abs(safe_number(signal.get("ATR")))

    if atr == 0:
        atr = abs(safe_number(signal.get("atr")))

    ema20 = safe_number(signal.get("EMA20"))
    ema50 = safe_number(signal.get("EMA50"))
    ema200 = safe_number(signal.get("EMA200"))

    rsi = safe_number(signal.get("RSI"))
    adx = safe_number(signal.get("ADX"))
    di_plus = safe_number(signal.get("DI_plus"))
    di_minus = safe_number(signal.get("DI_minus"))

    body_ratio = safe_number(signal.get("body_ratio"))
    range_ratio = safe_number(signal.get("range_ratio"))
    close_position = safe_number(signal.get("close_position"))
    score = safe_number(signal.get("score"))

    direction = str(
        signal.get("direction", "")
    ).upper()

    trend = str(
        signal.get("trend", "")
    ).upper()

    matching = signal.get("matching_strategies", [])

    if not isinstance(matching, list):
        matching = []

    trend_value = 1.0 if trend in (
        "UP", "BULLISH", "UPTREND", "BULL"
    ) else -1.0 if trend in (
        "DOWN", "BEARISH", "DOWNTREND", "BEAR"
    ) else 0.0

    direction_value = (
        1.0 if direction == "CALL"
        else -1.0 if direction == "PUT"
        else 0.0
    )

    atr_percent = (
        atr / price * 100.0
        if price > 0 else 0.0
    )

    ema20_distance = (
        (price - ema20) / price * 100.0
        if price > 0 and ema20 else 0.0
    )

    ema50_distance = (
        (price - ema50) / price * 100.0
        if price > 0 and ema50 else 0.0
    )

    ema200_distance = (
        (price - ema200) / price * 100.0
        if price > 0 and ema200 else 0.0
    )

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
        clamp(len(set(matching)) / 4.0, 0.0, 1.0),
    ]

    return features


def read_training_data(history_file=None):
    """قراءة الإشارات ونتائج الأفقين دون تكرار النتيجة الأساسية."""

    path = Path(history_file) if history_file else HISTORY_FILE

    if not path.exists():
        return {60: [], 120: []}

    signals = {}
    labels = {
        60: {},
        120: {}
    }

    try:
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue

                event_type = event.get("event")
                signal_id = event.get("signal_id")

                if not signal_id:
                    continue

                if event_type == "signal":
                    signal = event.get("signal")

                    if isinstance(signal, dict):
                        signals[signal_id] = signal

                elif event_type == "horizon_result":
                    try:
                        horizon = int(
                            event.get("horizon_seconds")
                        )
                    except (TypeError, ValueError):
                        continue

                    if horizon not in HORIZONS:
                        continue

                    outcome = str(
                        event.get("outcome", "")
                    ).upper()

                    if outcome not in ("WIN", "LOSS"):
                        # DRAW لا يدخل في تدريب نموذج الفوز والخسارة
                        continue

                    labels[horizon][signal_id] = outcome

    except OSError:
        logger.exception("Could not read AI training history")
        return {60: [], 120: []}

    datasets = {
        60: [],
        120: []
    }

    for horizon in HORIZONS:
        for signal_id, outcome in labels[horizon].items():
            signal = signals.get(signal_id)

            if not signal:
                continue

            try:
                features = extract_features(signal)
            except Exception:
                logger.exception(
                    "Could not extract AI features for %s",
                    signal_id
                )
                continue

            target = 1.0 if outcome == "WIN" else 0.0

            datasets[horizon].append(
                (features, target)
            )

    return datasets


def sigmoid(value):
    value = clamp(value, -30.0, 30.0)
    return 1.0 / (1.0 + math.exp(-value))


def train_model(dataset):
    """تدريب Logistic Regression باستخدام Python فقط."""

    total = len(dataset)

    wins = sum(
        1 for _, target in dataset
        if target == 1.0
    )

    losses = total - wins

    if total < MIN_TRAINING_SAMPLES:
        return None, {
            "status": "INSUFFICIENT_DATA",
            "samples": total,
            "wins": wins,
            "losses": losses
        }

    if wins < MIN_CLASS_SAMPLES or losses < MIN_CLASS_SAMPLES:
        return None, {
            "status": "CLASS_IMBALANCE",
            "samples": total,
            "wins": wins,
            "losses": losses
        }

    weights = [0.0] * (FEATURE_COUNT + 1)

    for epoch in range(TRAINING_EPOCHS):
        gradients = [0.0] * len(weights)

        for features, target in dataset:
            prediction = sigmoid(
                weights[0] + sum(
                    weights[index + 1] * features[index]
                    for index in range(FEATURE_COUNT)
                )
            )

            error = prediction - target

            gradients[0] += error

            for index, value in enumerate(features):
                gradients[index + 1] += error * value

        for index in range(len(weights)):
            gradient = gradients[index] / total

            if index > 0:
                gradient += (
                    L2_REGULARIZATION * weights[index]
                )

            weights[index] -= LEARNING_RATE * gradient

    return weights, {
        "status": "READY",
        "samples": total,
        "wins": wins,
        "losses": losses
    }


def predict_probability(weights, features):
    value = weights[0]

    for index, feature in enumerate(features):
        value += weights[index + 1] * feature

    return sigmoid(value)


def evaluate_signal(signal, history_file=None):
    """
    تقييم ظلّي للإشارة.
    لا يمنع الإشارة ولا يغير اتجاهها أو مدتها.
    """

    result = {
        "mode": "SHADOW",
        "model": "LogisticRegression",
        "predictions": {}
    }

    try:
        features = extract_features(signal)
        datasets = read_training_data(history_file)

        for horizon in HORIZONS:
            weights, info = train_model(
                datasets[horizon]
            )

            if weights is None:
                result["predictions"][str(horizon)] = {
                    "status": info["status"],
                    "samples": info["samples"],
                    "wins": info["wins"],
                    "losses": info["losses"],
                    "win_probability": None
                }
                continue

            probability = predict_probability(
                weights,
                features
            )

            result["predictions"][str(horizon)] = {
                "status": "READY",
                "samples": info["samples"],
                "wins": info["wins"],
                "losses": info["losses"],
                "win_probability": round(
                    probability * 100.0,
                    2
                )
            }

    except Exception:
        logger.exception("AI shadow evaluation failed")

        result["error"] = "EVALUATION_FAILED"

    return result
