import json
import logging
import math
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

# تقييم زمني: التدريب على الأقدم، والتحقق على الأحدث.
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


def _strategy_names(value):
    """
    تطبيع قائمة الاستراتيجيات بأمان.
    يدعم النصوص والقواميس دون استخدام set() على عناصر غير قابلة للتجزئة.
    """
    if not isinstance(value, (list, tuple, set)):
        return []

    names = []

    for item in value:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(
                item.get("name")
                or item.get("strategy")
                or item.get("id")
                or ""
            ).strip()
        else:
            name = str(item).strip() if item is not None else ""

        if name:
            names.append(name.casefold())

    return list(dict.fromkeys(names))


def extract_features(signal):
    """
    استخراج 14 خاصية رقمية ثابتة من الإشارة.
    يحافظ على نفس عدد الخصائص حتى تبقى سجلات JSONL القديمة قابلة للتدريب.
    """
    if not isinstance(signal, dict):
        raise TypeError("signal must be a dictionary")

    price = abs(safe_number(signal.get("price")))

    atr = abs(safe_number(
        signal.get("atr", signal.get("ATR"))
    ))

    ema20 = safe_number(
        signal.get("ema20", signal.get("EMA20"))
    )

    ema50 = safe_number(
        signal.get("ema50", signal.get("EMA50"))
    )

    ema200 = safe_number(
        signal.get("ema200", signal.get("EMA200"))
    )

    rsi = safe_number(
        signal.get("rsi", signal.get("RSI"))
    )

    adx = safe_number(
        signal.get("adx", signal.get("ADX"))
    )

    di_plus = safe_number(
        signal.get("plus_di", signal.get("DI_plus"))
    )

    di_minus = safe_number(
        signal.get("minus_di", signal.get("DI_minus"))
    )

    body_ratio = safe_number(signal.get("body_ratio"))
    range_ratio = safe_number(signal.get("range_ratio"))
    close_position = safe_number(signal.get("close_position"))
    score = safe_number(signal.get("score"))

    direction = str(signal.get("direction", "")).upper().strip()
    trend = str(signal.get("trend", "")).upper().strip()

    matching = _strategy_names(
        signal.get("matching_strategies", [])
    )

    trend_value = (
        1.0
        if trend in ("UP", "BULLISH", "UPTREND", "BULL")
        else -1.0
        if trend in ("DOWN", "BEARISH", "DOWNTREND", "BEAR")
        else 0.0
    )

    direction_value = (
        1.0 if direction == "CALL"
        else -1.0 if direction == "PUT"
        else 0.0
    )

    atr_percent = (
        atr / price * 100.0
        if price > 0
        else 0.0
    )

    ema20_distance = (
        (price - ema20) / price * 100.0
        if price > 0 and ema20
        else 0.0
    )

    ema50_distance = (
        (price - ema50) / price * 100.0
        if price > 0 and ema50
        else 0.0
    )

    ema200_distance = (
        (price - ema200) / price * 100.0
        if price > 0 and ema200
        else 0.0
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
        clamp(len(matching) / 4.0, 0.0, 1.0),
    ]

    if len(features) != FEATURE_COUNT:
        raise ValueError(
            f"Expected {FEATURE_COUNT} features, got {len(features)}"
        )

    return features


def read_training_data(history_file=None):
    """
    يقرأ نتائج 60 و120 ثانية من سجل JSONL.

    لا يستخدم ai_shadow كتسمية تدريبية؛ التدريب يعتمد فقط على
    WIN/LOSS الفعلية المسجلة في horizon_result.
    """
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
            for line_number, line in enumerate(file, start=1):
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "Skipping malformed history line %s",
                        line_number
                    )
                    continue

                if not isinstance(event, dict):
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
                        horizon = int(event.get("horizon_seconds"))
                    except (TypeError, ValueError, OverflowError):
                        continue

                    if horizon not in HORIZONS:
                        continue

                    outcome = str(
                        event.get("outcome", "")
                    ).upper().strip()

                    if outcome not in ("WIN", "LOSS"):
                        # DRAW لا يدخل في تدريب WIN/LOSS.
                        continue

                    labels[horizon][signal_id] = outcome

    except OSError:
        logger.exception("Could not read AI training history")
        return {60: [], 120: []}

    datasets = {60: [], 120: []}

    for horizon in HORIZONS:
        for signal_id, outcome in labels[horizon].items():
            signal = signals.get(signal_id)

            if not isinstance(signal, dict):
                continue

            try:
                features = extract_features(signal)
            except Exception:
                logger.exception(
                    "Could not extract AI features for signal %s",
                    signal_id
                )
                continue

            target = 1.0 if outcome == "WIN" else 0.0
            datasets[horizon].append((features, target))

    return datasets


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


def _fit_logistic(dataset, min_samples, min_class_samples):
    total = len(dataset)

    wins = sum(
        1 for _, target in dataset
        if target == 1.0
    )
    losses = total - wins

    if total < min_samples:
        return None, {
            "status": "INSUFFICIENT_DATA",
            "samples": total,
            "wins": wins,
            "losses": losses
        }

    if wins < min_class_samples or losses < min_class_samples:
        return None, {
            "status": "CLASS_IMBALANCE",
            "samples": total,
            "wins": wins,
            "losses": losses
        }

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

    return weights, {
        "status": "READY",
        "samples": total,
        "wins": wins,
        "losses": losses
    }


def _validation_metrics(weights, dataset):
    total = len(dataset)

    if not total:
        return {
            "samples": 0,
            "accuracy": None,
            "brier_score": None,
            "log_loss": None
        }

    correct = 0
    brier_total = 0.0
    log_loss_total = 0.0
    epsilon = 1e-9

    for features, target in dataset:
        probability = predict_probability(weights, features)
        predicted_class = 1.0 if probability >= 0.5 else 0.0

        if predicted_class == target:
            correct += 1

        brier_total += (probability - target) ** 2

        safe_probability = clamp(
            probability,
            epsilon,
            1.0 - epsilon
        )

        log_loss_total += -(
            target * math.log(safe_probability)
            + (1.0 - target) * math.log(1.0 - safe_probability)
        )

    return {
        "samples": total,
        "accuracy": round(correct / total * 100.0, 2),
        "brier_score": round(brier_total / total, 5),
        "log_loss": round(log_loss_total / total, 5)
    }


def _temporal_validation(dataset):
    """
    اختبار زمني بسيط: الأقدم للتدريب والأحدث للتحقق.
    لا نخلط ترتيب النتائج عشوائياً حتى لا تتسرب معلومات مستقبلية.
    """
    total = len(dataset)
    validation_count = max(
        MIN_VALIDATION_SAMPLES,
        int(total * VALIDATION_FRACTION)
    )

    split_index = total - validation_count

    if split_index < VALIDATION_MIN_TRAINING_SAMPLES:
        return {
            "status": "NOT_ENOUGH_DATA",
            "samples": 0,
            "accuracy": None,
            "brier_score": None,
            "log_loss": None
        }

    training_part = dataset[:split_index]
    validation_part = dataset[split_index:]

    weights, info = _fit_logistic(
        training_part,
        VALIDATION_MIN_TRAINING_SAMPLES,
        VALIDATION_MIN_CLASS_SAMPLES
    )

    if weights is None:
        return {
            "status": info["status"],
            "samples": 0,
            "accuracy": None,
            "brier_score": None,
            "log_loss": None
        }

    metrics = _validation_metrics(weights, validation_part)
    metrics["status"] = "READY"
    metrics["training_samples"] = len(training_part)

    return metrics


def train_model(dataset):
    """
    يتحقق زمنياً على أحدث جزء من البيانات، ثم يدرب النموذج النهائي
    على كل النتائج المكتملة المتاحة لإنتاج التوقع التالي.
    """
    validation = _temporal_validation(dataset)

    weights, info = _fit_logistic(
        dataset,
        MIN_TRAINING_SAMPLES,
        MIN_CLASS_SAMPLES
    )

    info["validation"] = validation

    return weights, info


def evaluate_signal(signal, history_file=None):
    """
    تقييم الإشارة في Shadow Mode فقط.

    لا يغير:
    - اتجاه الإشارة.
    - الاستراتيجية.
    - مدة الصفقة.
    - قرار إرسال الإشارة.

    win_probability تقدير النموذج، وليس ضماناً للربح.
    """
    result = {
        "mode": "SHADOW",
        "model": "LogisticRegression",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "predictions": {}
    }

    try:
        features = extract_features(signal)
        datasets = read_training_data(history_file)

        for horizon in HORIZONS:
            weights, info = train_model(datasets[horizon])

            prediction = {
                "status": info["status"],
                "samples": info["samples"],
                "wins": info["wins"],
                "losses": info["losses"],
                "win_probability": None,
                "validation": info.get("validation", {})
            }

            if weights is not None:
                probability = predict_probability(weights, features)

                prediction["status"] = "READY"
                prediction["win_probability"] = round(
                    probability * 100.0,
                    2
                )

            result["predictions"][str(horizon)] = prediction

    except Exception:
        logger.exception("AI shadow evaluation failed")
        result["error"] = "EVALUATION_FAILED"

    return result
