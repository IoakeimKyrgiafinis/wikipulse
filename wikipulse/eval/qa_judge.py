"""The LLM judge of the QA evaluation, and the file its judgements go to.

Two judgements per answer, each with its own prompt in `wikipulse/eval/prompts/`:

- **correctness** (`correctness_v1.md`): the answer against the question's expected
  answer, scored 0 (wrong), 1 (partly correct) or 2 (correct). Refusing is the correct
  answer to an unanswerable question.
- **faithfulness** (`faithfulness_v1.md`): every claim of the answer (each sentence or
  list item, as `wikipulse.qa.answer.claims()` splits it) against the facts that
  retrieval handed to the answering model: supported or not. The score is the share of
  supported claims. Refusals make no claims and are not judged.

Judgements are labels, so they go to `evals/labels/qa_judgements.jsonl`: append-only,
one judgement per line, with its labeler (`llm` with the model and prompt version, or
`human`). A human judgement is never overwritten; `qa.py rate` adds them.
"""

import hashlib
import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, Field

from wikipulse.eval.qa_questions import QAQuestion
from wikipulse.llm import LLMClient

logger = logging.getLogger(__name__)

DEFAULT_JUDGEMENTS_PATH = Path("evals") / "labels" / "qa_judgements.jsonl"
PROMPTS_DIR = Path(__file__).parent / "prompts"
CORRECTNESS_PROMPT = "correctness_v1"
FAITHFULNESS_PROMPT = "faithfulness_v1"
CLAIMS_PER_CALL = 10  # long answers are judged in batches, so the reply stays short
MAX_FACT_CHARS = 400

Metric = Literal["correctness", "faithfulness"]
Labeler = Literal["llm", "human"]

CORRECTNESS_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "score": {"type": "integer", "enum": [0, 1, 2]},
    },
    "required": ["reason", "score"],
}
FAITHFULNESS_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "integer"},
                    "reason": {"type": "string"},
                    "supported": {"type": "boolean"},
                },
                "required": ["claim", "reason", "supported"],
            },
        }
    },
    "required": ["claims"],
}


def load_prompt(version: str) -> str:
    """A judge prompt of the given version."""
    return (PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8")


def answer_hash(answerable: bool, fact_text: str) -> str:
    """Identifies one answer: a judgement is about this exact text."""
    return hashlib.sha256(f"{answerable}\n{fact_text}".encode()).hexdigest()[:16]


def reference_hash(question: QAQuestion) -> str:
    """Identifies what a correctness judgement compared with: when a question's expected
    answer is corrected, its old correctness judgements no longer count."""
    text = "\n".join([question.question, str(question.answerable), question.expected_answer])
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class ClaimVerdict(BaseModel):
    claim: str
    supported: bool | None  # None: the judge gave no verdict for it
    reason: str = ""


class QAJudgement(BaseModel):
    """One line of the judgements file."""

    run: str  # the evaluation run whose answer was judged
    question_id: str
    answer_hash: str
    reference_hash: str = ""  # correctness only: see `reference_hash()`
    metric: Metric
    score: float | None  # correctness 0, 1, 2; faithfulness share of supported claims
    claims: list[ClaimVerdict] = Field(default_factory=list)  # faithfulness only
    labeler: Labeler
    model: str | None = None  # for LLM judgements, e.g. "ollama:qwen3:8b"
    prompt_version: str | None = None
    reason: str = ""
    labeled_at: AwareDatetime  # wall clock, metadata only

    def judge(self) -> str:
        """Who judged: "human" or the model setting."""
        return "human" if self.labeler == "human" else str(self.model)


def load_judgements(path: Path) -> list[QAJudgement]:
    """The judgements stored in a file (none if it doesn't exist)."""
    if not path.exists():
        return []
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(QAJudgement.model_validate_json(line))
        except ValueError as error:
            logger.warning("Skipping bad line %d of %s: %s", number, path, error)
    return records


def append_judgement(path: Path, record: QAJudgement) -> None:
    """Add one record at the end of the file (never rewrites existing lines)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
    with path.open("a", encoding="utf-8", newline="\n") as out:
        out.write(line + "\n")


def find_judgement(
    records: Sequence[QAJudgement],
    run: str,
    question_id: str,
    hash_: str,
    metric: Metric,
    judge: str,
    prompt_version: str | None = None,
    reference: str | None = None,
) -> QAJudgement | None:
    """The latest judgement of this answer by `judge` ("human" or a model setting),
    made with `prompt_version` and against the expected answer `reference` (if given)."""
    matches = [
        r
        for r in records
        if r.run == run
        and r.question_id == question_id
        and r.answer_hash == hash_
        and r.metric == metric
        and r.judge() == judge
        and (prompt_version is None or r.labeler == "human" or r.prompt_version == prompt_version)
        and (reference is None or r.reference_hash == reference)
    ]
    return matches[-1] if matches else None


# --- What the judge sees ----------------------------------------------------------------


def correctness_message(question: QAQuestion, answer: str, answered: bool) -> str:
    """The correctness judge's input: question, reference and answer."""
    status = "answerable" if question.answerable else "not answerable"
    shown = answer if answered else f"(The system refused.) {answer}"
    return (
        f"QUESTION: {question.question}\n\n"
        f"REFERENCE ({status}): {question.expected_answer}\n\n"
        f"ANSWER: {shown}"
    )


def short(text: str, limit: int = MAX_FACT_CHARS) -> str:
    """`text` cut to `limit` characters."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def faithfulness_message(
    question: str, facts: Sequence[tuple[str, str]], claims: Sequence[str]
) -> str:
    """The faithfulness judge's input: question, facts and claims."""
    fact_lines = "\n".join(f"[{fact_id}] {short(text)}" for fact_id, text in facts)
    claim_lines = "\n".join(f"{number}. {claim}" for number, claim in enumerate(claims, 1))
    return f"QUESTION: {question}\n\nFACTS:\n{fact_lines}\n\nCLAIMS:\n{claim_lines}"


# --- Reading the judge's answers ----------------------------------------------------------


def parse_correctness(data: Any) -> tuple[int, str]:
    """(score, reason) from the parsed JSON answer; ValueError if it isn't valid."""
    if not isinstance(data, dict):
        raise ValueError(f"answer is not a JSON object: {data!r}")
    score = data.get("score")
    if isinstance(score, str) and score.strip().isdigit():
        score = int(score.strip())
    if isinstance(score, bool) or score not in (0, 1, 2):
        raise ValueError(f"score must be 0, 1 or 2, not {score!r}")
    return int(score), str(data.get("reason", "")).strip()


def parse_faithfulness(data: Any, claims: Sequence[str]) -> list[ClaimVerdict]:
    """One verdict per claim, in order; a claim the judge skipped gets `supported=None`
    (and the first verdict counts if it numbered a claim twice)."""
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        raise ValueError(f"answer has no list of claims: {data!r}")
    verdicts: dict[int, ClaimVerdict] = {}
    for item in data["claims"]:
        if not isinstance(item, dict):
            continue
        number, supported = item.get("claim"), item.get("supported")
        if not isinstance(number, int) or not 1 <= number <= len(claims):
            continue
        if number in verdicts or not isinstance(supported, bool):
            continue
        reason = str(item.get("reason", "")).strip()
        verdicts[number] = ClaimVerdict(
            claim=claims[number - 1], supported=supported, reason=reason
        )
    return [
        verdicts.get(number, ClaimVerdict(claim=claim, supported=None))
        for number, claim in enumerate(claims, 1)
    ]


def faithfulness_score(verdicts: Sequence[ClaimVerdict]) -> float | None:
    """Share of supported claims among those judged (None if none were)."""
    judged = [v.supported for v in verdicts if v.supported is not None]
    return sum(judged) / len(judged) if judged else None


# --- The judge ------------------------------------------------------------------------


class QAJudge:
    """Asks one LLM for correctness and faithfulness (temperature 0, no thinking)."""

    def __init__(self, client: LLMClient) -> None:
        self.client = client
        self.correctness_prompt = load_prompt(CORRECTNESS_PROMPT)
        self.faithfulness_prompt = load_prompt(FAITHFULNESS_PROMPT)

    @property
    def model(self) -> str:
        return self.client.setting

    def correctness(self, question: QAQuestion, answer: str, answered: bool) -> tuple[int, str]:
        response = self.client.chat(
            correctness_message(question, answer, answered),
            system=self.correctness_prompt,
            json_schema=CORRECTNESS_SCHEMA,
            temperature=0,
            think=False,
        )
        return parse_correctness(response.data)

    def faithfulness(
        self, question: str, facts: Sequence[tuple[str, str]], claims: Sequence[str]
    ) -> list[ClaimVerdict]:
        verdicts: list[ClaimVerdict] = []
        for start in range(0, len(claims), CLAIMS_PER_CALL):
            batch = list(claims[start : start + CLAIMS_PER_CALL])
            response = self.client.chat(
                faithfulness_message(question, facts, batch),
                system=self.faithfulness_prompt,
                json_schema=FAITHFULNESS_SCHEMA,
                temperature=0,
                think=False,
            )
            verdicts += parse_faithfulness(response.data, batch)
        return verdicts
