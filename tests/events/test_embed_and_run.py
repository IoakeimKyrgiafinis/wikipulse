"""The embedder (with a fake model) and the runner's pure helpers."""

from datetime import UTC, datetime

import numpy as np

from wikipulse.db.events import SpikeRow
from wikipulse.events.embed import Embedder, page_text
from wikipulse.events.run import ready_rows, row_text, to_pages


class FakeEncoder:
    """Maps a text to a fixed vector by its first letter; counts what it was asked."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, sentences: list[str], **kwargs) -> np.ndarray:
        self.calls.append(list(sentences))
        assert kwargs.get("normalize_embeddings") is True
        vectors = []
        for text in sentences:
            vector = np.zeros(3, dtype=np.float32)
            vector[ord(text[0]) % 3] = 1.0
            vectors.append(vector)
        return np.stack(vectors)


def row(spike_id: int, title: str, enriched: bool = True, **page) -> SpikeRow:
    values = {
        "page_status": "ok" if enriched else None,
        "canonical_title": None,
        "description": None,
        "summary": None,
        "links": None,
    }
    values.update(page)
    return SpikeRow(
        spike_id=spike_id,
        title=title,
        started_at=datetime(2026, 11, 5, tzinfo=UTC),
        ended_at=None,
        score=5.0,
        total_edits=5,
        enriched=enriched,
        **values,
    )


def test_page_text_joins_title_description_and_summary() -> None:
    assert page_text("Larry (cat)", "Chief Mouser", "Larry is a cat.") == (
        "Larry (cat). Chief Mouser. Larry is a cat."
    )
    assert page_text("Larry (cat)", None, "") == "Larry (cat)"
    assert len(page_text("x", None, "y" * 5000)) == 2000


def test_embedder_encodes_each_text_once_in_one_batch() -> None:
    encoder = FakeEncoder()
    embedder = Embedder("fake-model", encoder)
    vectors = embedder.embed_many(["apple", "banana", "apple"])
    assert encoder.calls == [["apple", "banana"]]
    assert np.array_equal(vectors[0], vectors[2])
    embedder.embed("apple")
    assert len(encoder.calls) == 1  # remembered


def test_row_text_and_pages_use_the_canonical_title_and_links() -> None:
    redirect = row(1, "Susan Wiles", canonical_title="Susie Wiles", summary="Chief of staff.")
    plain = row(2, "Not enriched yet", enriched=False)
    assert row_text(redirect) == "Susie Wiles. Chief of staff."
    assert row_text(plain) == "Not enriched yet"
    pages = to_pages(
        [redirect, row(3, "Pennsylvania", links=["Philadelphia"])], Embedder("f", FakeEncoder())
    )
    assert pages[0].canonical_title == "Susie Wiles" and pages[0].names == {
        "Susan Wiles",
        "Susie Wiles",
    }
    assert pages[1].links == {"Philadelphia"} and pages[1].embedding is not None


def test_a_spike_without_enrichment_waits_and_holds_back_later_ones() -> None:
    rows = [row(1, "A"), row(2, "B", enriched=False), row(3, "C")]
    seen: dict[int, float] = {}
    assert [r.spike_id for r in ready_rows(rows, seen, now=100.0, wait_s=120)] == [1]
    assert [r.spike_id for r in ready_rows(rows[1:], seen, now=200.0, wait_s=120)] == []
    assert [r.spike_id for r in ready_rows(rows[1:], seen, now=220.0, wait_s=120)] == [2, 3]
