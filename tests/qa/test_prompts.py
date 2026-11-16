"""The Cypher prompts: every version loads, every example is a valid read query, and the
current prompt's few-shot examples don't leak the QA evaluation (spec 12a)."""

import re
from pathlib import Path

import pytest

from wikipulse.eval.qa_questions import DEFAULT_QUESTIONS_PATH, QAQuestion, load_questions
from wikipulse.qa.cypher import PROMPT_VERSION, PROMPT_VERSIONS, load_prompt, validate_cypher

ROOT = Path(__file__).resolve().parents[2]
QUESTIONS = ROOT / DEFAULT_QUESTIONS_PATH
# Prompts written after the evaluation set existed. cypher_v1 (05a) predates it and is
# kept unchanged as the baseline 05b measured; one of its examples is the question
# "What happened today?", which 05b later also asked (see the 12a learning note).
CHECKED_FOR_LEAKS = [v for v in PROMPT_VERSIONS if v != "cypher_v1"]

_CODE_BLOCK = re.compile(r"```\n(.*?)```", re.DOTALL)
_QUOTED = re.compile(r'"([^"]+)"')
_PARTS = re.compile(r'"[^"]*"|[.;:,()]\s|[.;:,()]$|\s+and\s+')
_NAME = re.compile(r"[A-Z][\w'&.–-]*(?:\s+(?:(?:of|the|de|del|&)\s+)?[A-Z0-9][\w'&.–-]*)*")
# Capitalized words of the expected answers' own phrasing, not names of the graph.
_GENERIC = {"A", "All", "Also", "The", "They", "Entities", "Organizations", "People"}
_GENERIC |= {"UTC", "Wikipedia", "Wikidata ID"}


def normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def answer_names(question: QAQuestion) -> set[str]:
    """Names in an expected answer: the quoted event titles and every capitalized name
    ("Ueno Bank", "United States Senate"), without the answers' own phrasing."""
    text = question.expected_answer
    names = {title for title in _QUOTED.findall(text)}
    for part in _PARTS.split(text):
        for found in _NAME.finditer(part or ""):
            name = found.group(0).strip(" .'")
            if len(name) > 2 and name not in _GENERIC:
                names.add(name)
    return names


def leaks(prompt: str, questions: list[QAQuestion]) -> list[str]:
    """Every evaluation question, expected answer, answer name or expected event key
    that appears in `prompt`."""
    found = []
    flat = normalized(prompt)
    for question in questions:
        if normalized(question.question) in flat:
            found.append(f"{question.id} question: {question.question}")
        if normalized(question.expected_answer) in flat:
            found.append(f"{question.id} expected answer")
        for name in sorted(answer_names(question)):
            if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", prompt):
                found.append(f"{question.id} answer name: {name}")
        for key in question.expected_events:
            if key in prompt:
                found.append(f"{question.id} expected event: {key}")
    return found


@pytest.fixture(scope="module")
def questions() -> list[QAQuestion]:
    return load_questions(QUESTIONS)


def test_the_current_default_is_the_newest_version_and_the_old_one_stays_selectable() -> None:
    assert PROMPT_VERSION == "cypher_v2"
    for version in PROMPT_VERSIONS:
        assert "INVOLVED_IN" in load_prompt(version)
    with pytest.raises(ValueError, match="unknown Cypher prompt"):
        load_prompt("cypher_v0")


@pytest.mark.parametrize("version", PROMPT_VERSIONS)
def test_every_example_query_is_a_valid_read_query(version: str) -> None:
    examples = _CODE_BLOCK.findall(load_prompt(version))
    assert len(examples) >= 4
    for example in examples:
        validate_cypher(example)  # raises if the prompt teaches something the checks refuse


def test_the_new_prompt_shows_durations_and_exact_name_matching() -> None:
    prompt = load_prompt("cypher_v2")
    examples = "\n".join(_CODE_BLOCK.findall(prompt))
    assert "$today_start + duration(" in examples and "$now - duration(" in examples
    assert "toLower(n.name) = '" in examples and "n.aliases" in examples


def test_the_leak_check_finds_questions_answers_names_and_event_keys(
    questions: list[QAQuestion],
) -> None:
    q = next(q for q in questions if q.id == "q20")
    prompt = (
        f"Question: {q.question.upper()}\n"
        f"An example about {sorted(answer_names(q))[0]}.\n"
        f"key {q.expected_events[0]}\n{q.expected_answer}"
    )
    found = leaks(prompt, questions)
    assert any("q20 question" in f for f in found)
    assert any("q20 answer name" in f for f in found)
    assert any("q20 expected event" in f for f in found)
    assert any("q20 expected answer" in f for f in found)
    assert "Democratic Party" in answer_names(q)
    assert leaks("Acme Bank and the Riverside flood, in the past 45 minutes.", questions) == []


@pytest.mark.parametrize("version", CHECKED_FOR_LEAKS)
def test_the_prompt_does_not_contain_the_evaluation_questions_or_answers(
    version: str, questions: list[QAQuestion]
) -> None:
    assert len(questions) == 25
    assert leaks(load_prompt(version), questions) == []
