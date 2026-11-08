"""Text-to-Cypher: the LLM writes a query, and nothing runs before it is checked.

A generated query is untrusted input. Three layers keep it harmless:

1. `validate_cypher()` (here) rejects anything that is not a single read query: write
   and admin clauses (CREATE, MERGE, SET, DELETE, CALL, LOAD CSV, ...), several
   statements, unbounded variable-length patterns. It also forces a result limit:
   a missing LIMIT is added, a larger one lowered.
2. `QAGraph.run_query()` runs it in a READ transaction, which Neo4j itself refuses to
   write in, with a transaction timeout, and reads at most the limit of rows.
3. Results are only ever shown to the answering model as facts; they are never executed.

The checks work on the query with its strings, comments and `quoted` names blanked out,
so `WHERE n.name = 'Set'` is fine while a real SET clause is not.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from wikipulse.llm import LLMClient

logger = logging.getLogger(__name__)

PROMPT_VERSION = "cypher_v1"
PROMPTS_DIR = Path(__file__).parent / "prompts"
MAX_QUERY_CHARS = 3000
MAX_HOPS = 6  # longest variable-length pattern allowed, e.g. -[*1..6]-

# Clauses that write, call procedures, or administer the database. CALL is refused as a
# whole: procedures can write, and CALL { ... } IN TRANSACTIONS is a write tool too.
FORBIDDEN_WORDS = (
    "CREATE",
    "MERGE",
    "DELETE",
    "DETACH",
    "SET",
    "REMOVE",
    "DROP",
    "FOREACH",
    "LOAD",
    "CALL",
    "USE",
    "ALTER",
    "GRANT",
    "DENY",
    "REVOKE",
    "SHOW",
    "TERMINATE",
    "START",
    "STOP",
    "ENABLE",
    "RENAME",
)
READ_STARTS = ("MATCH", "OPTIONAL", "WITH", "UNWIND", "RETURN")

_COMMENT_OR_TEXT = re.compile(
    r"//[^\n]*"  # line comment
    r"|/\*.*?\*/"  # block comment
    r"|'(?:\\.|[^'\\])*'"  # 'string'
    r'|"(?:\\.|[^"\\])*"'  # "string"
    r"|`[^`]*`",  # `quoted name`
    re.DOTALL,
)
_WORD = re.compile(r"(?<![.\w$])([A-Za-z]+)(?!\w)")
_REL_PATTERN = re.compile(r"-\s*\[([^\]]*)\]")
_HOPS = re.compile(r"\*\s*(\d*)\s*(\.\.\s*(\d*))?")
_QUANTIFIER = re.compile(r"\)\s*(\{\s*\d*\s*,\s*\d*\s*\}|[+*])")
_FINAL_LIMIT = re.compile(r"\bLIMIT\s+(\S+)\s*$", re.IGNORECASE)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


class UnsafeCypherError(ValueError):
    """A generated query that must not run, with the reason."""


def blank_out(query: str) -> str:
    """The query with comments removed and strings and `quoted` names emptied."""

    def replace(match: re.Match[str]) -> str:
        text = match.group(0)
        if text.startswith(("//", "/*")):
            return " "
        return text[0] + text[-1]  # '' "" ``

    return _COMMENT_OR_TEXT.sub(replace, query)


def _check_hops(skeleton: str) -> None:
    for pattern in _REL_PATTERN.findall(skeleton):
        hops = _HOPS.search(pattern)
        if hops is None:
            continue
        lower, dots, upper = hops.group(1), hops.group(2), hops.group(3)
        if dots is None:
            limit = lower  # *3 means exactly 3; a bare * means any number
        else:
            limit = upper
        if not limit:
            raise UnsafeCypherError(
                f"variable-length pattern [{pattern.strip()}] needs an upper bound "
                f"(at most {MAX_HOPS}, e.g. *1..3)"
            )
        if int(limit) > MAX_HOPS:
            raise UnsafeCypherError(f"variable-length pattern longer than {MAX_HOPS} hops")
    if _QUANTIFIER.search(skeleton):
        raise UnsafeCypherError("quantified path patterns are not allowed")


def _with_limit(query: str, max_rows: int) -> str:
    """`query` with a final LIMIT of at most `max_rows`."""
    found = _FINAL_LIMIT.search(query)
    if found is None:
        return f"{query}\nLIMIT {max_rows}"
    value = found.group(1)
    if value.isdigit() and int(value) <= max_rows:
        return query
    return query[: found.start()] + f"LIMIT {max_rows}"


def validate_cypher(query: str, max_rows: int = 25) -> str:
    """The query ready to run, or `UnsafeCypherError` saying why it can't run."""
    query = _FENCE.sub("", query.strip()).strip().rstrip(";").strip()
    if not query:
        raise UnsafeCypherError("empty query")
    if len(query) > MAX_QUERY_CHARS:
        raise UnsafeCypherError(f"query longer than {MAX_QUERY_CHARS} characters")
    skeleton = blank_out(query)
    if ";" in skeleton:
        raise UnsafeCypherError("only one statement is allowed")
    words = [word.upper() for word in _WORD.findall(skeleton)]
    if not words or words[0] not in READ_STARTS:
        raise UnsafeCypherError(f"a read query starts with one of {', '.join(READ_STARTS)}")
    forbidden = sorted({word for word in words if word in FORBIDDEN_WORDS})
    if forbidden:
        raise UnsafeCypherError(f"not a read-only query (uses {', '.join(forbidden)})")
    if "RETURN" not in words:
        raise UnsafeCypherError("the query returns nothing (no RETURN)")
    _check_hops(skeleton)
    # A LIMIT inside the query (e.g. after a WITH) is fine, but the final result always
    # gets its own.
    return _with_limit(query, max_rows)


# --- Generation ------------------------------------------------------------------------


def load_prompt(version: str = PROMPT_VERSION) -> str:
    return (PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8")


CYPHER_SCHEMA = {
    "type": "object",
    "properties": {"cypher": {"type": "string"}},
    "required": ["cypher"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class TimeParameters:
    """The query parameters for "now", "today" and "this week", in event time."""

    now: datetime
    today_start: datetime
    week_start: datetime

    def as_params(self) -> dict[str, datetime]:
        return {"now": self.now, "today_start": self.today_start, "week_start": self.week_start}


def build_message(question: str, times: TimeParameters, max_rows: int) -> str:
    return (
        f"Current time (the newest event in the graph): {times.now.isoformat()}\n"
        f"$today_start = {times.today_start.isoformat()}, "
        f"$week_start = {times.week_start.isoformat()}\n"
        f"Return at most {max_rows} rows.\n\n"
        f"Question: {question}"
    )


class CypherWriter:
    """Asks the LLM for one read-only Cypher query that helps answer a question."""

    def __init__(self, client: LLMClient, prompt_version: str = PROMPT_VERSION) -> None:
        self.client = client
        self.prompt_version = prompt_version
        self.prompt = load_prompt(prompt_version)

    def write(self, question: str, times: TimeParameters, max_rows: int = 25) -> str:
        """The model's query (unchecked; may be empty when no query would help)."""
        response = self.client.chat(
            build_message(question, times, max_rows),
            system=self.prompt,
            json_schema=CYPHER_SCHEMA,
            temperature=0,
        )
        data = response.data if isinstance(response.data, dict) else {}
        query = str(data.get("cypher", "")).strip()
        logger.debug("Generated Cypher for %r: %s", question, query)
        return query
