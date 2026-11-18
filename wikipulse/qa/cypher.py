"""Text-to-Cypher: the LLM writes a query, and nothing runs before it is checked.

A generated query is untrusted input. Three layers keep it harmless:

1. `validate_cypher()` (here) rejects anything that is not a single read query: write
   and admin clauses (CREATE, MERGE, SET, DELETE, CALL, LOAD CSV, GQL's INSERT, ...),
   several statements (";" or GQL's NEXT), and paths without an upper bound of at
   most `MAX_HOPS` (`-[*]-`, `-[*2..]-`, quantified patterns such as `-[:R]-+`, `-->*`
   or `{1,}`, and so any SHORTEST search without a bound). A "+" or "*" after a group without any
   relationship, as in `date($today_start) + duration({days: 1})`, is arithmetic and
   allowed. It also forces a result limit on every branch of a UNION: a missing LIMIT
   is added, a larger one lowered.
2. `QAGraph.run_query()` runs it in a READ transaction, which Neo4j itself refuses to
   write in, with a transaction timeout, and reads at most the limit of rows.
3. Results are only ever shown to the answering model as facts; they are never executed.

The checks work on the query with its strings, comments and `quoted` names blanked out,
so `WHERE n.name = 'Set'` is fine while a real SET clause is not.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from wikipulse.llm import LLMClient, Message

logger = logging.getLogger(__name__)

PROMPT_VERSION = "cypher_v2"  # the default; QA_CYPHER_PROMPT chooses another
PROMPT_VERSIONS = ("cypher_v1", "cypher_v2")
MAX_ERROR_CHARS = 400  # of an error message shown to the model in a repair request
PROMPTS_DIR = Path(__file__).parent / "prompts"
MAX_QUERY_CHARS = 3000
MAX_HOPS = 6  # longest path pattern allowed, e.g. -[*1..6]- or -[:R]-{1,6}

# Clauses that write, call procedures, or administer the database. CALL is refused as a
# whole: procedures can write, and CALL { ... } IN TRANSACTIONS is a write tool too.
# GQL, the ISO standard Cypher is converging on, adds INSERT (a CREATE), NODETACH DELETE
# and NEXT (which chains a second statement onto the first). Neo4j's newer Cypher and
# Relate, the browser demo's engine (spec 13a), accept them, so they are refused too.
# GQL's read-only additions (LET, FILTER, FOR, FINISH) stay allowed.
FORBIDDEN_WORDS = (
    "CREATE",
    "INSERT",
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
    "NODETACH",
    "NEXT",
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
# A quantifier after a group "(...)" or a relationship ("-[...]-", "-->", "<--"):
# + (one or more), * (any number), {n}, {n,m}, {n,} or {,m}.
_QUANTIFIER = re.compile(r"[-)>]\s*(\{\s*(\d*)\s*(,\s*(\d*))?\s*\}|\+|\*)")
# Any piece of a relationship pattern: --, -[, ]-, ->, <- (Cypher allows spaces between).
_RELATIONSHIP = re.compile(r"-\s*-|-\s*\[|\]\s*-|-\s*>|<\s*-")
_LIMIT = re.compile(r"\bLIMIT\b", re.IGNORECASE)
_UNION = re.compile(r"\bUNION(\s+ALL)?\b", re.IGNORECASE)
# Clauses that can follow a LIMIT; when one does, that LIMIT is not the final one.
_CLAUSE = re.compile(
    r"\b(MATCH|OPTIONAL|WITH|UNWIND|RETURN|WHERE|ORDER|SKIP|OFFSET|UNION)\b", re.IGNORECASE
)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
# Neo4j's parser also accepts these characters as the "-", "<" and ">" of a
# relationship (e.g. an en dash: `(a)–[*]–(b)`), so the path checks see them as ASCII.
_ARROW_CHARS = str.maketrans(
    {
        **dict.fromkeys("\u00ad\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d", "-"),
        **dict.fromkeys("\u27e8\u3008\ufe64\uff1c", "<"),
        **dict.fromkeys("\u27e9\u3009\ufe65\uff1e", ">"),
    }
)


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


def mask(query: str) -> str:
    """Like `blank_out`, but every character keeps its place (comments and the inside of
    strings become spaces), so a position in the mask is the same position in `query`."""

    def replace(match: re.Match[str]) -> str:
        text = match.group(0)
        if text.startswith(("//", "/*")):
            return re.sub(r"[^\n]", " ", text)
        return text[0] + " " * (len(text) - 2) + text[-1]

    return _COMMENT_OR_TEXT.sub(replace, query)


def _depths(masked: str) -> list[int]:
    """The bracket depth at each character: 0 outside any (), [] or {}."""
    depths, depth = [], 0
    for char in masked:
        if char in ")]}":
            depth -= 1
        depths.append(depth)
        if char in "([{":
            depth += 1
    return depths


def _group_start(skeleton: str, close: int) -> int | None:
    """Where the "(" that the ")" at `close` closes is, or None if it is unbalanced."""
    depth = 0
    for index in range(close, -1, -1):
        if skeleton[index] == ")":
            depth += 1
        elif skeleton[index] == "(":
            depth -= 1
            if depth == 0:
                return index
    return None


def _is_arithmetic(skeleton: str, found: re.Match[str]) -> bool:
    """True when a "+", "*" or "{" after ")" belongs to an expression, not a path.

    A quantified path pattern repeats a group that holds at least one relationship
    (`((a)-->(b))+`; Neo4j requires one). A group without any relationship, such as the
    call `date($today_start)` in `date($today_start) + duration({days: 1})` or
    `count(*)` in `count(*) * 2`, can't be one, so the symbol after it is arithmetic.
    When in doubt (an unbalanced bracket, any arrow inside), the quantifier is checked.
    """
    if skeleton[found.start()] != ")":
        return False
    start = _group_start(skeleton, found.start())
    if start is None:
        return False
    return _RELATIONSHIP.search(skeleton[start : found.start() + 1]) is None


def _check_quantifiers(skeleton: str) -> None:
    for found in _QUANTIFIER.finditer(skeleton):
        if _is_arithmetic(skeleton, found):
            continue
        symbol, lower, comma, upper = found.groups()
        limit = None if symbol in "+*" else (upper if comma else lower)
        if not limit:
            raise UnsafeCypherError(
                f"quantified path pattern {found.group(0).strip()!r} needs an upper bound "
                f"(at most {MAX_HOPS}, e.g. {{1,3}})"
            )
        if int(limit) > MAX_HOPS:
            raise UnsafeCypherError(f"quantified path pattern longer than {MAX_HOPS} hops")


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
    _check_quantifiers(skeleton)


def _branches(masked: str) -> list[tuple[int, int]]:
    """Start and end of each part of a UNION (one part without UNION)."""
    depths = _depths(masked)
    starts, ends = [0], []
    for union in _UNION.finditer(masked):
        if depths[union.start()] == 0:
            ends.append(union.start())
            starts.append(union.end())
    return list(zip(starts, [*ends, len(masked)], strict=True))


def _limit_branch(text: str, masked: str, max_rows: int) -> str:
    """One query part (`text`, with its mask) with a final LIMIT of at most `max_rows`."""
    depths = _depths(masked)
    limits = [m for m in _LIMIT.finditer(masked) if depths[m.start()] == 0]
    last = limits[-1] if limits else None
    if last is not None and not _CLAUSE.search(masked[last.end() :]):
        value = masked[last.end() :].strip()
        # isdecimal, not isdigit: int() can't read a digit like "²" that isdigit accepts.
        if value.isdecimal() and int(value) <= max_rows:
            return text
        trailing = text[len(text.rstrip()) :]
        return text[: last.start()] + f"LIMIT {max_rows}" + trailing
    body = text.rstrip()
    return body + f"\nLIMIT {max_rows}" + text[len(body) :]


def _with_limit(query: str, max_rows: int) -> str:
    """`query` with a final LIMIT of at most `max_rows` on every branch of a UNION.

    The LIMIT is looked for in the mask, so one inside a comment or a string does not
    count, and neither does one inside a subquery's braces."""
    masked = mask(query)
    for start, end in reversed(_branches(masked)):
        limited = _limit_branch(query[start:end], masked[start:end], max_rows)
        query = query[:start] + limited + query[end:]
    return query


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
    _check_hops(skeleton.translate(_ARROW_CHARS))
    # A LIMIT inside the query (e.g. after a WITH) is fine, but the final result always
    # gets its own.
    return _with_limit(query, max_rows)


# --- Generation ------------------------------------------------------------------------


def load_prompt(version: str = PROMPT_VERSION) -> str:
    """The Cypher prompt of the given version."""
    if version not in PROMPT_VERSIONS:
        raise ValueError(f"unknown Cypher prompt {version!r} (one of {', '.join(PROMPT_VERSIONS)})")
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
    """The Cypher model's input: the time parameters and the question."""
    return (
        f"Current time (the newest event in the graph): {times.now.isoformat()}\n"
        f"$today_start = {times.today_start.isoformat()}, "
        f"$week_start = {times.week_start.isoformat()}\n"
        f"Return at most {max_rows} rows.\n\n"
        f"Question: {question}"
    )


def repair_message(error: str) -> str:
    """The follow-up that asks the model to fix a query that could not run."""
    error = " ".join(error.split())
    if len(error) > MAX_ERROR_CHARS:
        error = error[: MAX_ERROR_CHARS - 3] + "..."
    return (
        f"That query could not run: {error}\n"
        "Write a corrected query for the same question, following the rules and the "
        "examples. If no query over this graph can help, answer with an empty string."
    )


class CypherWriter:
    """Asks the LLM for one read-only Cypher query that helps answer a question, and,
    when that query can't run, for one corrected version (`repair`)."""

    def __init__(self, client: LLMClient, prompt_version: str = PROMPT_VERSION) -> None:
        self.client = client
        self.prompt_version = prompt_version
        self.prompt = load_prompt(prompt_version)

    def write(self, question: str, times: TimeParameters, max_rows: int = 25) -> str:
        """The model's query (unchecked; may be empty when no query would help)."""
        return self._ask([Message.user(build_message(question, times, max_rows))], question)

    def repair(
        self, question: str, times: TimeParameters, max_rows: int, query: str, error: str
    ) -> str:
        """A corrected query, after `query` was rejected or failed with `error`.

        The model sees its own earlier answer and the error, as a short conversation."""
        conversation = [
            Message.user(build_message(question, times, max_rows)),
            Message.assistant(json.dumps({"cypher": query}, ensure_ascii=False)),
            Message.user(repair_message(error)),
        ]
        return self._ask(conversation, question)

    def _ask(self, conversation: list[Message], question: str) -> str:
        response = self.client.chat(
            conversation, system=self.prompt, json_schema=CYPHER_SCHEMA, temperature=0
        )
        data = response.data if isinstance(response.data, dict) else {}
        query = str(data.get("cypher", "")).strip()
        logger.debug("Generated Cypher for %r: %s", question, query)
        return query
