"""Pair labels for the grouping evaluation: are two spikes the same news story?

The labels file `evals/labels/grouping_labels.jsonl` holds one judgement per line,
about one *pair* of spikes: `same`, `different` or `unsure`, with its labeler (`llm` or
`human`). Like the spike labels it is append-only, and a human label beats an LLM label
(`pick_pair_label`). A pair of labeled spikes is a grouping in its smallest form: a
labeled grouping of n pages is the same as its n(n-1)/2 pair labels.

The LLM labeler sees what a human would: both pages' titles, short descriptions and
summaries (from the `pages` table, no network) and when each spike happened.
"""

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel

from wikipulse.llm import LLMClient

logger = logging.getLogger(__name__)

DEFAULT_PAIR_LABELS_PATH = Path("evals") / "labels" / "grouping_labels.jsonl"
PAIR_LABELS = ("same", "different", "unsure")
PairLabelValue = Literal["same", "different", "unsure"]
Labeler = Literal["llm", "human"]
MAX_SUMMARY_CHARS = 1000

SpikeKey = tuple[str, datetime]  # (spike title, spike started_at): unique per detector run
PairKey = tuple[SpikeKey, SpikeKey]  # the earlier spike first


def pair_key(a: SpikeKey, b: SpikeKey) -> PairKey:
    """The pair in a fixed order (earlier start first, then title)."""
    return (a, b) if (a[1], a[0]) <= (b[1], b[0]) else (b, a)


class PairLabelRecord(BaseModel):
    """One line of the pair labels file."""

    title_a: str
    started_a: AwareDatetime
    title_b: str
    started_b: AwareDatetime
    label: PairLabelValue
    labeler: Labeler
    model: str | None = None  # for LLM labels, e.g. "ollama:qwen3:4b"
    prompt_version: int | None = None  # for LLM labels: PROMPT_VERSION (None means 1)
    reason: str = ""
    spike_run_id: str  # the detector run the spikes come from
    sampled_for: str = ""  # the grouping run whose sample asked for this label
    stratum: str = ""
    labeled_at: AwareDatetime  # wall clock, metadata only

    @property
    def key(self) -> PairKey:
        return pair_key((self.title_a, self.started_a), (self.title_b, self.started_b))


def load_pair_records(path: Path) -> list[PairLabelRecord]:
    """All records, oldest first. A missing file means no labels yet."""
    if not path.exists():
        return []
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(PairLabelRecord.model_validate_json(line))
        except ValueError as error:
            logger.warning("Skipping bad line %d of %s: %s", number, path, error)
    return records


def append_pair_record(path: Path, record: PairLabelRecord) -> None:
    """Add one record at the end of the file (never rewrites existing lines)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
    with path.open("a", encoding="utf-8", newline="\n") as out:
        out.write(line + "\n")


def pick_pair_label(records: Sequence[PairLabelRecord]) -> PairLabelRecord | None:
    """The label that counts for one pair: the latest human one, else the latest LLM one."""
    humans = [r for r in records if r.labeler == "human"]
    chosen = humans or list(records)
    return chosen[-1] if chosen else None


def labels_by_pair(records: Sequence[PairLabelRecord]) -> dict[PairKey, PairLabelRecord]:
    grouped: dict[PairKey, list[PairLabelRecord]] = {}
    for record in records:
        grouped.setdefault(record.key, []).append(record)
    return {key: label for key, rs in grouped.items() if (label := pick_pair_label(rs))}


# --- What a labeler sees ------------------------------------------------------------


@dataclass(frozen=True)
class PageView:
    """One side of a pair, as shown to a labeler."""

    title: str  # the spike's title
    canonical_title: str
    started_at: datetime
    total_edits: int
    description: str | None
    summary: str | None

    @property
    def key(self) -> SpikeKey:
        return (self.title, self.started_at)

    def as_text(self, name: str) -> str:
        summary = (self.summary or "(no summary)")[:MAX_SUMMARY_CHARS]
        return (
            f"{name}: {self.canonical_title}\n"
            f"Short description: {self.description or '(none)'}\n"
            f"Burst of editing started {self.started_at:%Y-%m-%d %H:%M} UTC "
            f"({self.total_edits} edits)\n"
            f"Summary: {summary}"
        )


def pair_text(a: PageView, b: PageView) -> str:
    return f"{a.as_text('Page A')}\n\n{b.as_text('Page B')}"


# Version of SYSTEM_PROMPT, stored with every LLM label. Version 1 described the
# election rule only in passing, and qwen3:4b called most pairs of one election night
# "different"; version 2 states the rule first, with examples.
PROMPT_VERSION = 2

SYSTEM_PROMPT = """\
Two English Wikipedia articles each had a sudden burst of editing on the same day.
Decide whether the two bursts belong to the SAME NEWS STORY: the same real-world event
or development, the way a news site would put them under one headline or one live blog.

Judge at the level of the news event, not of the article. Different offices, races,
states and candidates of ONE election day are the SAME story: "2026 United States Senate
election in Maine", "2026 Ohio gubernatorial election", "2026 United States House of
Representatives elections in Iowa" and a newly elected representative all belong to the
story "the 2026 US elections".

Also SAME: an election and its candidates, results or the office at stake; a death and
the person's article and the list of deaths of that year; a match and the teams or
players in it; a release and the film, its cast or studio; a disaster and the place it
hit; a scandal and the people involved; two bursts on the same article on the same day.

DIFFERENT: two separate events, even of the same kind (the deaths of two different
people, two different matches, two unrelated TV episodes), or articles that are only
related in general (same country, same field, same historical period) without one
current event connecting both bursts.

UNSURE: the information is not enough to tell.

The day of the bursts is "today"; your own knowledge may end earlier, so trust the
summaries over your memory.

Answer with JSON: {"label": "same" | "different" | "unsure", "reason": "<one sentence>"}.
"""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": list(PAIR_LABELS)},
        "reason": {"type": "string"},
    },
    "required": ["label", "reason"],
}


def parse_pair_answer(content: str) -> tuple[str, str]:
    """(label, reason) from the model's JSON answer; ValueError if it isn't valid."""
    answer = json.loads(content)
    label = str(answer.get("label", "")).strip().lower()
    if label not in PAIR_LABELS:
        raise ValueError(f"unknown label {label!r}")
    return label, str(answer.get("reason", "")).strip()


class LLMPairLabeler:
    """Asks an LLM whether a pair is one story (thinking off, temperature 0, JSON schema).

    The client (`wikipulse.llm`) checks the answer against `ANSWER_SCHEMA` and asks once
    more if it isn't valid, then raises `StructuredOutputError` (a ValueError).
    """

    def __init__(self, client: LLMClient) -> None:
        self.client = client

    def close(self) -> None:
        self.client.close()

    def label(self, a: PageView, b: PageView) -> tuple[str, str]:
        """(label, reason) for one pair."""
        response = self.client.chat(
            pair_text(a, b),
            system=SYSTEM_PROMPT,
            json_schema=ANSWER_SCHEMA,
            temperature=0,
            think=False,
        )
        return parse_pair_answer(response.text)


# --- Labeling loops -----------------------------------------------------------------


@dataclass(frozen=True)
class PairToLabel:
    a: PageView
    b: PageView
    stratum: str

    @property
    def key(self) -> PairKey:
        return pair_key(self.a.key, self.b.key)


def needs_llm_label(key: PairKey, records: Sequence[PairLabelRecord], model: str) -> bool:
    """False if a human labeled the pair, or the same model did with the current prompt."""
    return not any(
        r.key == key
        and (
            r.labeler == "human" or (r.model == model and (r.prompt_version or 1) == PROMPT_VERSION)
        )
        for r in records
    )


def make_record(
    pair: PairToLabel,
    label: str,
    labeler: Labeler,
    spike_run_id: str,
    sampled_for: str,
    labeled_at: datetime,
    model: str | None = None,
    reason: str = "",
) -> PairLabelRecord:
    (title_a, started_a), (title_b, started_b) = pair.key
    return PairLabelRecord(
        title_a=title_a,
        started_a=started_a,
        title_b=title_b,
        started_b=started_b,
        label=label,
        labeler=labeler,
        model=model,
        prompt_version=PROMPT_VERSION if labeler == "llm" else None,
        reason=reason,
        spike_run_id=spike_run_id,
        sampled_for=sampled_for,
        stratum=pair.stratum,
        labeled_at=labeled_at,
    )


def label_pairs_with_llm(
    pairs: Sequence[PairToLabel],
    labeler: LLMPairLabeler,
    model_setting: str,
    spike_run_id: str,
    sampled_for: str,
    path: Path,
    now: Callable[[], datetime],
) -> int:
    """Label every sampled pair that needs it; returns how many were labeled."""
    records = load_pair_records(path)
    done = 0
    for number, pair in enumerate(pairs, start=1):
        names = f"{pair.a.title} / {pair.b.title}"
        if not needs_llm_label(pair.key, records, model_setting):
            logger.debug("[%d/%d] %s: already labeled", number, len(pairs), names)
            continue
        label, reason = labeler.label(pair.a, pair.b)
        record = make_record(
            pair, label, "llm", spike_run_id, sampled_for, now(), model_setting, reason
        )
        append_pair_record(path, record)
        records.append(record)
        done += 1
        logger.info("[%d/%d] %s: %s (%s)", number, len(pairs), names, label, reason)
    return done


KEYS = {"s": "same", "d": "different", "u": "unsure"}


def label_pairs_manually(
    pairs: Sequence[PairToLabel],
    spike_run_id: str,
    sampled_for: str,
    path: Path,
    now: Callable[[], datetime],
    ask: Callable[[str], str] = input,
    show: Callable[[str], None] = print,
) -> tuple[int, int, int]:
    """Terminal labeling. Returns (labeled, compared with an LLM label, agreements).

    Pairs a human already labeled are skipped; the LLM's answer is shown only after
    yours, so it can't anchor you.
    """
    records = load_pair_records(path)
    done_by_human = {r.key for r in records if r.labeler == "human"}
    todo = [p for p in pairs if p.key not in done_by_human]
    show(f"{len(todo)} of {len(pairs)} sampled pairs have no human label yet.")
    labeled = compared = agreed = 0
    for number, pair in enumerate(todo, start=1):
        show("\n" + "=" * 78 + f"\n[{number}/{len(todo)}]\n" + pair_text(pair.a, pair.b))
        answer = ""
        while answer not in {*KEYS, "k", "q"}:
            answer = ask("same story (s), different (d), unsure (u), skip (k), quit (q)? ")
            answer = answer.strip().lower()
        if answer == "q":
            break
        if answer == "k":
            continue
        note = ask("Optional note: ").strip()
        record = make_record(
            pair, KEYS[answer], "human", spike_run_id, sampled_for, now(), reason=note
        )
        append_pair_record(path, record)
        labeled += 1
        llm = [r.label for r in records if r.key == pair.key and r.labeler == "llm"]
        if llm:
            compared += 1
            agreed += llm[-1] == KEYS[answer]
            show(f"You: {KEYS[answer]}. The LLM said: {llm[-1]}.")
        records.append(record)
    if compared:
        show(f"\nYou agreed with the LLM on {agreed} of {compared} pairs.")
    return labeled, compared, agreed
