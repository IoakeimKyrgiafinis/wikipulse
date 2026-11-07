"""The label tool: sampling, context, the LLM call, and never overwriting human labels."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from tests.eval.helpers import at, spike
from wikipulse.config import Settings
from wikipulse.eval.label import (
    OllamaLabeler,
    SpikeContext,
    fetch_context,
    label_manually,
    label_with_llm,
    parse_answer,
    parse_comments,
    parse_model_setting,
    revisions_params,
    summary_url,
)
from wikipulse.eval.label_store import append_record, load_records, record_for
from wikipulse.eval.metrics import sample_spikes
from wikipulse.eval.wiki_client import WikiClient

FIXTURES = Path(__file__).parent.parent / "fixtures" / "eval"
MODEL = "ollama:qwen3:4b"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


SPIKES = [spike(f"Page {n:02d}", at(6, n * 0.25)) for n in range(80)]


def test_sample_is_reproducible_and_sized() -> None:
    first = sample_spikes(SPIKES, 50, seed=7)
    assert len(first) == 50
    assert first == sample_spikes(list(reversed(SPIKES)), 50, seed=7)  # input order irrelevant
    assert first != sample_spikes(SPIKES, 50, seed=8)
    assert first == sorted(first, key=lambda s: s.started_at)
    assert len(sample_spikes(SPIKES[:10], 50, seed=7)) == 10


def test_model_setting() -> None:
    assert parse_model_setting("ollama:qwen3:8b") == ("ollama", "qwen3:8b")
    with pytest.raises(ValueError):
        parse_model_setting("anthropic:claude")


def test_parse_answer() -> None:
    assert parse_answer('{"label": "Not News", "reason": "edit war"}') == ("not news", "edit war")
    with pytest.raises(ValueError):
        parse_answer('{"label": "maybe"}')
    with pytest.raises(ValueError):
        parse_answer("not json")


def soharwardy():
    return spike("Syed Soharwardy", datetime(2026, 11, 6, 20, 30, tzinfo=UTC), delay_min=20)


def test_context_from_saved_api_responses(tmp_path: Path) -> None:
    settings = Settings(_env_file=None)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "/page/summary/" in request.url.path:
            return httpx.Response(200, json=load("summary-Syed_Soharwardy.json"))
        return httpx.Response(200, json=load("revisions-Syed_Soharwardy.json"))

    transport = httpx.MockTransport(handler)
    with WikiClient("WikiPulse-test", tmp_path, 0, transport=transport) as client:
        context = fetch_context(client, settings, soharwardy())
    assert context.description == "Pakistani–Canadian Islamic scholar"
    assert "Islamic Supreme Council of Canada" in context.extract
    assert len(context.comments) == 7
    assert context.comments[1] == "/* Biography */"
    text = context.as_text()
    assert "Article: Syed Soharwardy" in text and "- added more info" in text
    assert seen[0].url.path.endswith("/page/summary/Syed_Soharwardy")


def test_missing_summary_still_gives_a_context(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/page/summary/" in request.url.path:
            return httpx.Response(404, json={"type": "not_found"})
        return httpx.Response(200, json={"query": {"pages": [{"missing": True}]}})

    transport = httpx.MockTransport(handler)
    with WikiClient("WikiPulse-test", tmp_path, 0, transport=transport) as client:
        context = fetch_context(client, Settings(_env_file=None), soharwardy())
    assert context.extract == "" and context.comments == []
    assert "(no summary available)" in context.as_text()


def test_revision_query_covers_the_burst() -> None:
    params = revisions_params(soharwardy())
    assert params["rvstart"] == "2026-11-06T21:31:00Z"  # end + 1 minute, newest first
    assert params["rvend"] == "2026-11-06T20:09:00Z"  # first edit - 1 minute
    assert "user" not in params["rvprop"]
    assert parse_comments({"query": {"pages": [{"revisions": [{"comment": ""}]}]}}) == []
    assert summary_url("https://x/api/rest_v1", "AC/DC live") == (
        "https://x/api/rest_v1/page/summary/AC%2FDC_live"
    )


def ollama_transport(answers: list[str], requests: list[dict], status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={"error": "model not found"})
        return httpx.Response(200, json={"message": {"content": answers.pop(0)}})

    return httpx.MockTransport(handler)


def test_ollama_request_has_schema_no_thinking_and_a_context_length() -> None:
    requests: list[dict] = []
    answers = ['{"label": "news", "reason": "an election"}']
    labeler = OllamaLabeler("http://ollama", "qwen3:4b", ollama_transport(answers, requests))
    assert labeler.label(SpikeContext(soharwardy())) == ("news", "an election")
    body = requests[0]
    assert body["think"] is False and body["stream"] is False
    assert body["options"] == {"temperature": 0, "num_ctx": 8192}
    assert body["format"]["properties"]["label"]["enum"] == ["news", "not news", "unsure"]
    assert body["messages"][0]["role"] == "system"


def test_ollama_invalid_answer_is_retried_once() -> None:
    requests: list[dict] = []
    answers = ["oops", '{"label": "unsure", "reason": "?"}']
    labeler = OllamaLabeler("http://ollama", "m", ollama_transport(answers, requests))
    assert labeler.label(SpikeContext(soharwardy()))[0] == "unsure"
    assert len(requests) == 2
    answers.extend(["oops", "still oops"])
    with pytest.raises(ValueError):
        labeler.label(SpikeContext(soharwardy()))


def test_missing_ollama_model_suggests_pulling_it() -> None:
    labeler = OllamaLabeler("http://ollama", "qwen3:8b", ollama_transport([], [], status=404))
    with pytest.raises(RuntimeError, match="ollama pull qwen3:8b"):
        labeler.label(SpikeContext(soharwardy()))


class FixedLabeler:
    def __init__(self, label: str = "news") -> None:
        self.calls = 0
        self.value = label

    def label(self, context: SpikeContext) -> tuple[str, str]:
        self.calls += 1
        return self.value, "fixed"


def test_llm_labels_are_appended_and_not_repeated(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    sample = SPIKES[:3]
    labeler = FixedLabeler()
    assert label_with_llm(sample, SpikeContext, labeler, MODEL, "loose", path) == 3
    assert label_with_llm(sample, SpikeContext, labeler, MODEL, "loose", path) == 0
    records = load_records(path)
    assert [r.labeler for r in records] == ["llm"] * 3
    assert {r.model for r in records} == {MODEL}
    assert labeler.calls == 3


def test_llm_never_overwrites_or_relabels_a_human_label(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    human = record_for(SPIKES[0], "loose", "not news", "human", NOW, reason="edit war")
    append_record(path, human)
    before = path.read_text(encoding="utf-8")
    labeler = FixedLabeler("news")
    assert label_with_llm(SPIKES[:2], SpikeContext, labeler, MODEL, "loose", path) == 1
    after = path.read_text(encoding="utf-8")
    assert after.startswith(before)  # the human line is untouched
    assert load_records(path)[0] == human


def test_manual_labeling_records_a_human_label_and_reports_agreement(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    label_with_llm(SPIKES[:3], SpikeContext, FixedLabeler("news"), MODEL, "loose", path)
    answers = iter(["?", "n", "", "x", "unclear: a TV episode?", "q"])
    shown: list[str] = []
    labeled, compared, agreed = label_manually(
        SPIKES[:3], SpikeContext, "loose", path, ask=lambda _: next(answers), show=shown.append
    )
    assert (labeled, compared, agreed) == (2, 2, 1)
    humans = [r for r in load_records(path) if r.labeler == "human"]
    assert [(r.title, r.label) for r in humans] == [("Page 00", "news"), ("Page 01", "not news")]
    assert humans[1].reason == "unclear: a TV episode?"
    assert any("agreed with the LLM on 1 of 2" in line for line in shown)

    # A second session only offers the spike without a human label.
    answers = iter(["u", ""])
    label_manually(SPIKES[:3], SpikeContext, "loose", path, ask=lambda _: next(answers), show=print)
    assert len([r for r in load_records(path) if r.labeler == "human"]) == 3
