"""Settings for the Agent Journal client on this machine."""

import json
from fcntl import LOCK_EX, flock

DEFAULT_EXPLANATION_QUALITY = "Contextual"
EXPLANATION_LEVELS = {
    "Basic": "Name the task, its purpose, and the main outcome in plain language. Resolve essential references.",
    "Contextual": "Explain the task on its own. Connect relevant earlier decisions to the work and its outcome.",
    "In-depth": "Explain why the work was needed, what earlier decisions or constraints shaped it, what was done, and what the evidence confirms or leaves open.",
}
QUALITY_ALIASES = {
    "basic": "Basic", "low": "Basic", "기본": "Basic", "낮음": "Basic",
    "contextual": "Contextual", "normal": "Contextual", "맥락": "Contextual", "보통": "Contextual",
    "in-depth": "In-depth", "high": "In-depth", "심층": "In-depth", "높음": "In-depth",
}


def quality_name(value):
    try:
        return QUALITY_ALIASES[value.strip().casefold()]
    except (AttributeError, KeyError):
        raise ValueError("Choose an explanation quality: Basic, Contextual, or In-depth.") from None


def load_settings(root):
    defaults = {
        "explanation_quality": DEFAULT_EXPLANATION_QUALITY,
        "explanation_levels": EXPLANATION_LEVELS.copy(),
        "measure_explanation_usage": True,
    }
    path = root / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return defaults
    if not isinstance(settings, dict):
        raise ValueError("Expected a JSON object in settings.json.")
    preferences = settings.get("explanation_levels", {})
    if not isinstance(preferences, dict) or any(
        key not in EXPLANATION_LEVELS or not isinstance(value, str) or not value.strip()
        for key, value in preferences.items()
    ):
        raise ValueError("Expected explanation level descriptions for Basic, Contextual, or In-depth.")
    settings = defaults | settings
    if not isinstance(settings["measure_explanation_usage"], bool):
        raise ValueError("measure_explanation_usage must be true or false.")
    settings["explanation_quality"] = quality_name(settings["explanation_quality"])
    settings["explanation_levels"] = EXPLANATION_LEVELS | preferences
    return settings


def update_settings(root, *, quality=None, measure_usage=None):
    from .client_state import write_state

    path = root / "settings.json"
    with (root / "settings.lock").open("a") as lock:
        flock(lock, LOCK_EX)
        settings = load_settings(root)
        if quality is not None:
            settings["explanation_quality"] = quality_name(quality)
        if measure_usage is not None:
            settings["measure_explanation_usage"] = measure_usage
        if quality is not None or measure_usage is not None or not path.exists():
            write_state(path, settings, durable=True, indent=2)
        return settings
