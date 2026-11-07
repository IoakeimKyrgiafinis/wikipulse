"""Summary embeddings for grouping: one unit-length vector per page.

An embedding model turns a text into a vector so that texts with similar meaning get
vectors pointing in similar directions. Here the model is small and local
(`EVENTS_EMBED_MODEL`, by default `BAAI/bge-small-en-v1.5`: 384 numbers per text, runs
on the CPU). It is downloaded from Hugging Face the first time and cached on this
machine. Vectors are normalized to length 1, so their dot product is the cosine
similarity.
"""

import logging
from collections.abc import Sequence
from typing import Protocol

import numpy as np

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 2000  # bge-small reads at most 512 tokens anyway
BATCH_SIZE = 32


def page_text(title: str, description: str | None, summary: str | None) -> str:
    """The text that represents a page: title, short description and summary."""
    parts = [title]
    if description:
        parts.append(description)
    if summary:
        parts.append(summary)
    return ". ".join(parts)[:MAX_TEXT_CHARS]


class TextEncoder(Protocol):
    """What we need from a sentence-transformers model (tests pass a fake)."""

    def encode(self, sentences: list[str], **kwargs) -> np.ndarray: ...


class Embedder:
    """Embeds texts with a sentence-transformers model, remembering every result."""

    def __init__(self, model_name: str, encoder: TextEncoder | None = None) -> None:
        self.model_name = model_name
        self._encoder = encoder
        self._cache: dict[str, np.ndarray] = {}

    @property
    def encoder(self) -> TextEncoder:
        if self._encoder is None:
            # Imported here: loading torch takes seconds and only the runner needs it.
            from sentence_transformers import SentenceTransformer

            logger.info("Loading embedding model %s", self.model_name)
            self._encoder = SentenceTransformer(self.model_name, device="cpu")
        return self._encoder

    def embed_many(self, texts: Sequence[str]) -> list[np.ndarray]:
        """Unit vectors for `texts`; new texts are encoded together in batches."""
        new = list(dict.fromkeys(t for t in texts if t not in self._cache))
        if new:
            vectors = self.encoder.encode(
                new, batch_size=BATCH_SIZE, normalize_embeddings=True, show_progress_bar=False
            )
            for text, vector in zip(new, vectors, strict=True):
                self._cache[text] = np.asarray(vector, dtype=np.float32)
        return [self._cache[t] for t in texts]

    def embed(self, text: str) -> np.ndarray:
        return self.embed_many([text])[0]
