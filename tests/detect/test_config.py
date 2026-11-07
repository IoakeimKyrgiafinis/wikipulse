"""Detector thresholds come from the DETECT_* settings."""

from wikipulse.config import DetectorConfig, Settings


def test_every_threshold_has_a_setting() -> None:
    settings_fields = set(Settings.model_fields)
    missing = [f for f in DetectorConfig.model_fields if f"detect_{f}" not in settings_fields]
    assert missing == []


def test_defaults_match_and_settings_override(monkeypatch) -> None:
    assert Settings(_env_file=None).detector_config() == DetectorConfig()
    monkeypatch.setenv("DETECT_MIN_EDITS", "7")
    monkeypatch.setenv("DETECT_COUNT_REVERTS", "true")
    config = Settings(_env_file=None).detector_config()
    assert (config.min_edits, config.count_reverts) == (7, True)
