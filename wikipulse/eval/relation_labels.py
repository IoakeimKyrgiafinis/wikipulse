"""Relation labels: is an extracted relationship what its source text says?

The labels file `evals/labels/relation_labels.jsonl` holds one judgement per line about
one extracted relationship: `correct`, `wrong` or `unsure`, with its labeler (`llm` or
`human`). Like the other label files it is append-only, and a human label beats an LLM
label (`pick_relation_label`), so a human label is never overwritten.

A relationship is identified by its event, its two entity keys (Wikidata IDs where
linked), its relation phrase and the revision it was read from. Two extractions of the
same event that produce the same fact share the label.

The judge sees what the extraction model saw for that page (the summary and the text
added during the burst, trimmed the same way), and the claim, never the rest of the
graph or Wikidata: the question is "does the source say this?", not "is it true?".
"""

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel

from wikipulse.eval.extraction_data import RelationView
from wikipulse.llm import LLMClient

logger = logging.getLogger(__name__)

DEFAULT_RELATION_LABELS_PATH = Path("evals") / "labels" / "relation_labels.jsonl"
RELATION_LABELS = ("correct", "wrong", "unsure")
RelationLabelValue = Literal["correct", "wrong", "unsure"]
Labeler = Literal["llm", "human"]
RelationKey = tuple[str, str, str, str, int | None]
DEFAULT_SAMPLE_SIZE = 50
DEFAULT_SEED = 2026


class RelationLabelRecord(BaseModel):
    """One line of the relation labels file."""

    event_key: str
    subject_key: str
    subject: str  # the entity's name, for reading the file
    relation: str
    object_key: str
    object: str
    source_title: str
    source_revision: int | None
    label: RelationLabelValue
    labeler: Labeler
    model: str | None = None  # for LLM labels, e.g. "ollama:qwen3:8b"
    prompt_version: int | None = None  # for LLM labels: JUDGE_PROMPT_VERSION
    reason: str = ""
    sampled_for: str = ""  # the evaluation whose sample asked for this label
    labeled_at: AwareDatetime  # wall clock, metadata only

    @property
    def key(self) -> RelationKey:
        return (
            self.event_key,
            self.subject_key,
            self.relation,
            self.object_key,
            self.source_revision,
        )


def load_relation_records(path: Path) -> list[RelationLabelRecord]:
    """All records, oldest first. A missing file means no labels yet."""
    if not path.exists():
        return []
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(RelationLabelRecord.model_validate_json(line))
        except ValueError as error:
            logger.warning("Skipping bad line %d of %s: %s", number, path, error)
    return records


def append_relation_record(path: Path, record: RelationLabelRecord) -> None:
    """Add one record at the end of the file (never rewrites existing lines)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
    with path.open("a", encoding="utf-8", newline="\n") as out:
        out.write(line + "\n")


def pick_relation_label(records: Sequence[RelationLabelRecord]) -> RelationLabelRecord | None:
    """The label that counts: the latest human one, else the latest LLM one."""
    humans = [r for r in records if r.labeler == "human"]
    chosen = humans or list(records)
    return chosen[-1] if chosen else None


def labels_by_relation(
    records: Sequence[RelationLabelRecord],
) -> dict[RelationKey, RelationLabelRecord]:
    grouped: dict[RelationKey, list[RelationLabelRecord]] = {}
    for record in records:
        grouped.setdefault(record.key, []).append(record)
    return {key: label for key, rs in grouped.items() if (label := pick_relation_label(rs))}


# --- The sample ---------------------------------------------------------------------


def sample_order(relation: RelationView, seed: int) -> str:
    """A fixed pseudo-random rank: the same relation always ranks the same, so two
    evaluations of overlapping extractions share most of their sample (and labels)."""
    text = "|".join(str(part) for part in (seed, *relation.key))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sample_relations(
    relations: Sequence[RelationView], size: int = DEFAULT_SAMPLE_SIZE, seed: int = DEFAULT_SEED
) -> list[RelationView]:
    """The first `size` relations in hash order."""
    return sorted(relations, key=lambda r: sample_order(r, seed))[:size]


# --- What a labeler sees ------------------------------------------------------------


@dataclass(frozen=True)
class SourceText:
    """The page a relation was read from, as the extraction model saw it."""

    title: str
    description: str | None
    summary: str | None
    changes: str


@dataclass(frozen=True)
class RelationToLabel:
    relation: RelationView
    subject: str  # entity names
    object: str
    subject_type: str
    object_type: str
    source: SourceText | None  # None if the page could not be read again

    @property
    def key(self) -> RelationKey:
        return self.relation.key

    def claim(self) -> str:
        return f'"{self.subject}" {self.relation.relation} "{self.object}"'


def with_article(word: str) -> str:
    return f"an {word}" if word[:1] in "aeiou" else f"a {word}"


def judge_message(item: RelationToLabel) -> str:
    source = item.source
    if source is None:
        text = "(the source could not be read)"
    else:
        text = (
            f"SOURCE: {source.title}\n"
            f"Short description: {source.description or '(none)'}\n"
            f"Summary: {source.summary or '(no summary)'}\n"
            f"Text added during the burst of edits:\n{source.changes or '(none available)'}"
        )
    return (
        f"{text}\n\n"
        f"CLAIM: {item.claim()}\n"
        f"({item.subject} is {with_article(item.subject_type)}, "
        f"{item.object} is {with_article(item.object_type)}.)"
    )


# Version of JUDGE_PROMPT, stored with every LLM label. Version 1 asked for the label
# first and listed the rules loosely; qwen3:8b called 48 of 50 relations of the 04b graph
# correct, including "Cyclone Murasu rescheduled elections for Repalle" (the election
# commission did) and "Ramayana: Part 1 based on Valmiki" (on his epic). Version 2 makes
# the judge check subject, object and relation one by one, and write its reason
# *before* the label (the JSON schema puts "reason" first), so the label follows from
# the check instead of the check justifying the label.
JUDGE_PROMPT_VERSION = 2

JUDGE_PROMPT = """\
You check facts that a program extracted from a Wikipedia article for a knowledge graph.
You get the SOURCE (the article's summary and the text added in a burst of edits) and
one CLAIM of the form "subject" relation "object". Read the claim literally, as the
English sentence: subject relation object.

Check three things against the SOURCE:
1. Subject: does the source say that the SUBJECT itself does or has this, and not
   someone or something else mentioned near it?
2. Object: is the OBJECT exactly what the source names, and not something related to it
   (a person instead of their work, a place instead of an organization there)?
3. Relation: does the relation say what the source says, in this direction, and is it a
   specific fact? Vague relations ("involves", "related to", "associated with",
   "connected to" when the source says more) and garbled sentences are wrong.

- "correct": all three hold. Different wording is fine ("won the election" supports
  "elected in").
- "wrong": any of the three fails, or the source does not say it.
- "unsure": the source is too unclear or too short to tell.

Judge only by the SOURCE. Do not use your own knowledge: the source may describe events
newer than what you know, and a claim that is true in general but not in the source is
"wrong".

Answer with JSON: {"reason": "<one or two sentences: the three checks>",
"label": "correct" | "wrong" | "unsure"}.
"""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "label": {"type": "string", "enum": list(RELATION_LABELS)},
    },
    "required": ["reason", "label"],
}


def answer_from_data(answer: Any) -> tuple[str, str]:
    """(label, reason) from the already parsed JSON answer; ValueError if it isn't valid."""
    if not isinstance(answer, dict):
        raise ValueError(f"answer is not a JSON object: {answer!r}")
    label = str(answer.get("label", "")).strip().lower()
    if label not in RELATION_LABELS:
        raise ValueError(f"unknown label {label!r}")
    return label, str(answer.get("reason", "")).strip()


class LLMRelationJudge:
    """Asks an LLM whether the source supports a relation (no thinking, temperature 0)."""

    def __init__(self, client: LLMClient) -> None:
        self.client = client

    def close(self) -> None:
        self.client.close()

    def label(self, item: RelationToLabel) -> tuple[str, str]:
        response = self.client.chat(
            judge_message(item),
            system=JUDGE_PROMPT,
            json_schema=ANSWER_SCHEMA,
            temperature=0,
            think=False,
        )
        return answer_from_data(response.data)


# --- Labeling loops -----------------------------------------------------------------


def needs_llm_label(key: RelationKey, records: Sequence[RelationLabelRecord], model: str) -> bool:
    """False if a human labeled it, or the same model did with the current prompt."""
    return not any(
        r.key == key
        and (
            r.labeler == "human" or (r.model == model and r.prompt_version == JUDGE_PROMPT_VERSION)
        )
        for r in records
    )


def make_record(
    item: RelationToLabel,
    label: str,
    labeler: Labeler,
    sampled_for: str,
    labeled_at: datetime,
    model: str | None = None,
    reason: str = "",
) -> RelationLabelRecord:
    relation = item.relation
    return RelationLabelRecord(
        event_key=relation.event_key,
        subject_key=relation.subject,
        subject=item.subject,
        relation=relation.relation,
        object_key=relation.object,
        object=item.object,
        source_title=relation.source_title,
        source_revision=relation.source_revision,
        label=label,
        labeler=labeler,
        model=model,
        prompt_version=JUDGE_PROMPT_VERSION if labeler == "llm" else None,
        reason=reason,
        sampled_for=sampled_for,
        labeled_at=labeled_at,
    )


def label_with_llm(
    items: Sequence[RelationToLabel],
    judge: LLMRelationJudge,
    model_setting: str,
    sampled_for: str,
    path: Path,
    now: Callable[[], datetime],
) -> int:
    """Label every sampled relation that needs it; returns how many were labeled."""
    records = load_relation_records(path)
    done = 0
    for number, item in enumerate(items, start=1):
        if not needs_llm_label(item.key, records, model_setting):
            logger.debug("[%d/%d] %s: already labeled", number, len(items), item.claim())
            continue
        label, reason = judge.label(item)
        record = make_record(item, label, "llm", sampled_for, now(), model_setting, reason)
        append_relation_record(path, record)
        records.append(record)
        done += 1
        logger.info("[%d/%d] %s: %s (%s)", number, len(items), item.claim(), label, reason)
    return done


KEYS = {"c": "correct", "w": "wrong", "u": "unsure"}


def label_manually(
    items: Sequence[RelationToLabel],
    sampled_for: str,
    path: Path,
    now: Callable[[], datetime],
    ask: Callable[[str], str] = input,
    show: Callable[[str], None] = print,
) -> tuple[int, int, int]:
    """Terminal labeling. Returns (labeled, compared with an LLM label, agreements).

    Relations a human already labeled are skipped; the LLM's answer is shown only after
    yours, so it can't anchor you.
    """
    records = load_relation_records(path)
    done_by_human = {r.key for r in records if r.labeler == "human"}
    todo = [item for item in items if item.key not in done_by_human]
    show(f"{len(todo)} of {len(items)} sampled relations have no human label yet.")
    labeled = compared = agreed = 0
    for number, item in enumerate(todo, start=1):
        show("\n" + "=" * 78 + f"\n[{number}/{len(todo)}]\n" + judge_message(item))
        answer = ""
        while answer not in {*KEYS, "k", "q"}:
            answer = ask("correct (c), wrong (w), unsure (u), skip (k), quit (q)? ")
            answer = answer.strip().lower()
        if answer == "q":
            break
        if answer == "k":
            continue
        note = ask("Optional note: ").strip()
        record = make_record(item, KEYS[answer], "human", sampled_for, now(), reason=note)
        append_relation_record(path, record)
        labeled += 1
        llm = [r.label for r in records if r.key == item.key and r.labeler == "llm"]
        if llm:
            compared += 1
            agreed += llm[-1] == KEYS[answer]
            show(f"You: {KEYS[answer]}. The LLM said: {llm[-1]}.")
        records.append(record)
    if compared:
        show(f"\nYou agreed with the LLM on {agreed} of {compared} relations.")
    return labeled, compared, agreed
