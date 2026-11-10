"""A tiny synthetic dataset in the shape of spec 07a's, for the training tests.

The label depends on a few features (busy the day before, a `{{Current}}` template, the
year in the title) plus noise, and on the summary text ("news" pages mention an event),
so every model can learn something. Some labels are unknown, some features NaN.
"""

import hashlib
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from wikipulse.events.embed import Embedder
from wikipulse.ml.features import FEATURE_NAMES

SIZES = {"train": 160, "valid": 60, "test": 80}
DIMENSIONS = 8


def synthetic_split(name: str, rows: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    features = rng.normal(0, 1, size=(rows, len(FEATURE_NAMES)))
    frame = pd.DataFrame(features, columns=FEATURE_NAMES)
    frame["prior_edits_24h"] = rng.poisson(10, rows).astype(float)
    frame["has_current_template"] = rng.integers(0, 2, rows).astype(float)
    frame["title_has_spike_year"] = rng.integers(0, 2, rows).astype(float)
    frame.loc[rng.random(rows) < 0.1, "page_age_days"] = np.nan
    signal = (
        0.15 * (frame["prior_edits_24h"] - 10)
        + 1.5 * frame["has_current_template"]
        + 1.0 * frame["title_has_spike_year"]
        - 1.0
    )
    label = (signal + rng.normal(0, 0.7, rows) > 0).astype(float)
    unknown = rng.random(rows) < 0.2
    start = datetime(2026, 11, 1, tzinfo=UTC) + timedelta(days=list(SIZES).index(name) * 3)
    frame["title"] = [f"{name} page {i}" for i in range(rows)]
    frame["started_at"] = [start + timedelta(minutes=7 * i) for i in range(rows)]
    frame["spike_id"] = [
        f"{t}|{s.isoformat()}" for t, s in zip(frame.title, frame.started_at, strict=True)
    ]
    frame["page_revision_id"] = pd.array(range(1000 * seed, 1000 * seed + rows), dtype="Int64")
    frame["default_rule_fired"] = (signal + rng.normal(0, 1.5, rows)) > 0
    frame["label"] = pd.array(np.where(unknown, np.nan, label), dtype="Int8")
    frame["label_source"] = np.where(unknown, "unknown", "llm")
    frame["summary_text"] = [
        f"{t}. {'An event that happened this week' if y == 1 else 'A place or a thing'}"
        for t, y in zip(frame.title, label, strict=True)
    ]
    return frame


def synthetic_frames(seed: int = 7) -> dict[str, pd.DataFrame]:
    return {
        name: synthetic_split(name, rows, seed + i) for i, (name, rows) in enumerate(SIZES.items())
    }


class FakeEncoder:
    """Deterministic 'embeddings': a direction for event texts, plus a hash of the text."""

    def encode(self, sentences: list[str], **kwargs) -> np.ndarray:
        vectors = []
        for text in sentences:
            digest = hashlib.sha256(text.encode()).digest()
            v = np.frombuffer(digest[:DIMENSIONS], dtype=np.uint8).astype(np.float32) / 255
            v[0] += 2.0 if "event" in text else 0.0
            vectors.append(v / np.linalg.norm(v))
        return np.vstack(vectors)


def fake_embedder() -> Embedder:
    return Embedder("fake-model", encoder=FakeEncoder())
