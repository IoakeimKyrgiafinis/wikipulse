"""The analyst agent: an LLM in a loop with tools, written without a framework.

    model --(tool calls)--> our code runs them --(results)--> model --> ... --> briefing

That is all an "agent" is. `AnalystAgent.run(event_id)`:

1. The code (not the model) calls `get_event` first, so every run starts from the
   event's facts; the call and its result go into the conversation as if the model had
   asked, which also shows a small model what a tool call looks like.
2. Loop: send the conversation and the tool list to the model (`wikipulse.llm`).
   - It answers with tool calls: run each one (`ToolBox.call`, with its timeout), append
     the results, go round again.
   - It answers with text: that is the draft briefing; the loop ends.
3. Controls, checked before every model turn: `max_steps` model turns, a token budget
   (input + output of every call), the run's wall-clock timeout, and the context window
   (stop before the conversation overflows it). Loop detection: the second identical
   call (same tool, same arguments) is not run again, the model gets the earlier result
   with a warning; the third ends the investigation. Whatever stops the loop, the model
   then gets one last turn *without tools* to write the briefing from what it has.
4. The draft is checked by code (`briefing.py`): links removed, unknown source ids
   removed, sentences without a source id sent back once, then dropped; citations of
   sources the run never read anything from are removed too. Then the grounding check
   (`verify`): in a fresh conversation, the model (thinking) gets each sentence with the
   text the run read from its sources and says which ones that text states; the others
   (and any sentence without an explicit verdict) are dropped.

The result, `AgentRun`, holds the briefing (or why there is none), every step, the
whole conversation and the token usage; `transcript.py` saves it as JSON.
"""

import asyncio
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from wikipulse.agent.briefing import (
    VERIFY_SCHEMA,
    Briefing,
    clean_draft,
    drop_unread_citations,
    finish,
    missing_citations,
    unsupported_sentences,
    verification_request,
)
from wikipulse.agent.sources import SourceBook
from wikipulse.agent.tools import ToolBox, ToolOutcome
from wikipulse.config import Settings
from wikipulse.llm import (
    LLMClient,
    LLMError,
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
    Usage,
    trace,
)

logger = logging.getLogger(__name__)

PROMPT_VERSION = "analyst_v1"
VERIFY_PROMPT_VERSION = "verify_v1"
VERIFY_MAX_TOKENS = 3000
PROMPTS_DIR = Path(__file__).parent / "prompts"
MAX_CALLS_PER_TURN = 4
CONTEXT_SHARE = 0.85  # stop when the next request would fill this much of the context
CHARS_PER_TOKEN = 3  # a cautious estimate for text the model has not read yet

TASK = "Investigate WikiPulse event {event_id} and write its briefing."
FINAL_PROMPT = (
    "{reason} Stop investigating now and write the briefing from the tool results above: "
    "three to five sentences, each ending with the source ids it uses, such as [F2]. "
    "No tool calls, no links."
)
RETRY_PROMPT = (
    "These sentences of your briefing have no source ids:\n{sentences}\n\n"
    "Write the whole briefing again. End every sentence with the ids of the sources it "
    "uses, exactly as the tool results show them (e.g. [F2]), and leave out any sentence "
    "that no source supports."
)
REPEAT_NOTE = (
    "You already called {name} with exactly these arguments; this is the same result "
    "again. Do not repeat calls: use another tool or write the briefing.\n\n{text}"
)
LOOP_NOTE = (
    "Not run: this is call number {count} of {name} with the same arguments. The "
    "investigation is over."
)
SKIPPED_NOTE = "Not run: {reason}"
REASONS = {
    "max_steps": "You have used all your tool turns.",
    "token_budget": "You have used the token budget of this investigation.",
    "timeout": "The time for this investigation is up.",
    "context_full": "The conversation is nearly as long as you can read.",
    "loop_detected": "You called the same tool with the same arguments three times.",
    "empty_answer": "Your last answer was empty.",
}

StopReason = Literal[
    "answered",
    "max_steps",
    "token_budget",
    "timeout",
    "context_full",
    "loop_detected",
    "empty_answer",
    "event_not_found",
    "llm_error",
]


def load_prompt(version: str = PROMPT_VERSION) -> str:
    return (PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8")


@dataclass(frozen=True)
class AgentLimits:
    """When the loop stops investigating (see the module docstring)."""

    max_steps: int = 8
    token_budget: int = 60_000
    run_timeout_s: float = 900
    context_tokens: int | None = 8192  # the model's context window; None: don't check
    repeat_limit: int = 3

    @classmethod
    def from_settings(cls, settings: Settings) -> "AgentLimits":
        return cls(
            max_steps=settings.agent_max_steps,
            token_budget=settings.agent_token_budget,
            run_timeout_s=settings.agent_run_timeout_s,
            context_tokens=settings.ollama_num_ctx
            if settings.agent_model.startswith("ollama:")
            else None,
        )


class Step(BaseModel):
    """One entry of the transcript: a model turn or a tool call."""

    kind: Literal["model", "tool", "final", "retry", "verify"]
    text: str = ""  # what the model wrote, or the tool result it was shown
    thinking: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    is_error: bool = False
    suspicious: bool = False
    timed_out: bool = False
    repeated: int = 0  # how many times this exact call had been made before
    usage: Usage | None = None
    seconds: float = 0.0


class AgentRun(BaseModel):
    """Everything about one investigation: the result and how it came about."""

    event_id: str  # as requested
    event_key: str | None = None  # as the graph knows it
    model: str
    prompt_version: str
    started_at: datetime  # wall clock: metadata only
    seconds: float = 0.0
    stop_reason: StopReason | None = None
    draft: str | None = None  # the model's last text before the checks
    briefing: Briefing | None = None
    unsupported: list[str] = Field(default_factory=list)  # removed by the grounding check
    verified: bool | None = None  # see `Briefing.verified`
    error: str | None = None
    usage: Usage = Field(default_factory=Usage)
    model_turns: int = 0
    tool_calls: int = 0
    suspicious_results: int = 0
    steps: list[Step] = Field(default_factory=list)
    sources: dict[str, list[str]] = Field(default_factory=dict)  # id -> "title, revision"
    system_prompt: str = ""
    messages: list[Message] = Field(default_factory=list)

    @property
    def tokens(self) -> int:
        return self.usage.input_tokens + self.usage.output_tokens


def signature(call: ToolCall) -> str:
    """Same tool and same arguments give the same signature (argument order ignored)."""
    return call.name + json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)


class AnalystAgent:
    """Investigates one event at a time with the tools of a `ToolBox`."""

    def __init__(
        self,
        client: LLMClient,
        tools: ToolBox,
        limits: AgentLimits | None = None,
        prompt_version: str = PROMPT_VERSION,
        think: bool = False,
        verify: bool = True,
    ) -> None:
        self.client = client
        self.tools = tools
        self.limits = limits or AgentLimits()
        self.prompt_version = prompt_version
        self.prompt = load_prompt(prompt_version)
        self.think = think
        self.verify = verify
        self.verify_prompt = load_prompt(VERIFY_PROMPT_VERSION)

    async def run(self, event_id: str) -> AgentRun:
        """Investigate `event_id` and return the run, with a briefing if one passed."""
        run = AgentRun(
            event_id=event_id,
            model=self.client.setting,
            prompt_version=self.prompt_version,
            started_at=datetime.now(UTC),
            system_prompt=self.prompt,
        )
        started = time.monotonic()
        book = SourceBook(event_id)
        messages: list[Message] = []
        # One trace per run (spec 09b): the model's turns and the tool calls inside it.
        with trace("agent run", as_type="agent", input={"event_id": event_id}) as observed:
            try:
                await self._investigate(run, book, messages, started)
            except LLMError as error:
                logger.warning("The agent's model failed on %s: %s", event_id, error)
                run.stop_reason, run.error = "llm_error", str(error)
            observed.update(
                output=run.briefing.as_text() if run.briefing else None,
                error=run.error,
                metadata={"stop_reason": run.stop_reason, "model_turns": run.model_turns},
            )
        run.messages = messages
        run.sources = {
            fact.id: [citation.label() for citation in fact.sources] for fact in book.context.facts
        }
        run.seconds = round(time.monotonic() - started, 3)
        return run

    async def _investigate(
        self, run: AgentRun, book: SourceBook, messages: list[Message], started: float
    ) -> None:
        specs = self.tools.specs or await self.tools.load()
        messages.append(Message.user(TASK.format(event_id=run.event_id)))
        first = ToolCall(id="call_event", name="get_event", arguments={"event_id": run.event_id})
        outcome = await self.tools.call(first, book)
        self._record_tool(run, first, outcome)
        if outcome.is_error:
            run.stop_reason, run.error = "event_not_found", outcome.text
            return
        run.event_key = ((outcome.data or {}).get("event") or {}).get("event_id")
        book.event_key = run.event_key  # page reads cite this event's revisions
        messages += [Message.assistant("", [first]), Message.tool_result(first, outcome.text)]
        reason = await self._loop(run, book, messages, specs, started)
        draft = run.draft
        if draft is None:
            nudge = FINAL_PROMPT.format(reason=REASONS.get(reason, ""))
            messages.append(Message.user(nudge))
            response = await self._chat(run, messages, None, "final")
            draft = response.text
            messages.append(Message.assistant(draft))
        run.stop_reason = reason
        run.draft = draft
        run.briefing = await self._check(run, book, messages, draft)

    async def _loop(
        self,
        run: AgentRun,
        book: SourceBook,
        messages: list[Message],
        specs: list[ToolSpec],
        started: float,
    ) -> StopReason:
        """Model turns with tools until a draft or a limit. Returns why it stopped."""
        seen: Counter[str] = Counter()
        results: dict[str, str] = {}
        pending = 0  # characters of tool results the model has not read yet
        while True:
            reason = self._limit_reached(run, started, pending)
            if reason is not None:
                return reason
            response = await self._chat(run, messages, specs, "model")
            if not response.tool_calls:
                if not response.text.strip():
                    return "empty_answer"
                run.draft = response.text
                messages.append(Message.assistant(response.text))
                return "answered"
            messages.append(Message.assistant(response.text, response.tool_calls))
            stop: StopReason | None = None
            pending = 0
            for n, call in enumerate(response.tool_calls):
                key = signature(call)
                seen[key] += 1
                if stop is not None or n >= MAX_CALLS_PER_TURN:
                    why = "the investigation is over." if stop else "too many calls in one turn."
                    text = SKIPPED_NOTE.format(reason=why)
                    self._record_tool(run, call, ToolOutcome(text, is_error=True), seen[key] - 1)
                elif seen[key] >= self.limits.repeat_limit:
                    stop = "loop_detected"
                    text = LOOP_NOTE.format(count=seen[key], name=call.name)
                    self._record_tool(run, call, ToolOutcome(text, is_error=True), seen[key] - 1)
                elif key in results:
                    text = REPEAT_NOTE.format(name=call.name, text=results[key])
                    self._record_tool(run, call, ToolOutcome(text), seen[key] - 1)
                else:
                    outcome = await self.tools.call(call, book)
                    text = results[key] = outcome.text
                    self._record_tool(run, call, outcome)
                messages.append(Message.tool_result(call, text))
                pending += len(text)
            if stop is not None:
                return stop

    def _limit_reached(self, run: AgentRun, started: float, pending: int) -> StopReason | None:
        limits = self.limits
        if run.model_turns >= limits.max_steps:
            return "max_steps"
        if run.tokens >= limits.token_budget:
            return "token_budget"
        if time.monotonic() - started >= limits.run_timeout_s:
            return "timeout"
        last = next((s.usage for s in reversed(run.steps) if s.usage is not None), None)
        if limits.context_tokens and last is not None:
            expected = last.input_tokens + last.output_tokens + pending // CHARS_PER_TOKEN
            if expected >= CONTEXT_SHARE * limits.context_tokens:
                return "context_full"
        return None

    async def _chat(
        self,
        run: AgentRun,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        kind: Literal["model", "final", "retry"],
    ) -> LLMResponse:
        response = await asyncio.to_thread(
            self.client.chat,
            list(messages),
            system=self.prompt,
            tools=tools,
            temperature=0,
            think=self.think,
        )
        run.usage = run.usage + response.usage
        if kind == "model":
            run.model_turns += 1
        run.steps.append(
            Step(
                kind=kind,
                text=response.text,
                thinking=response.thinking,
                tool_calls=response.tool_calls,
                usage=response.usage,
                seconds=round(response.usage.latency_s, 3),
            )
        )
        return response

    def _record_tool(
        self, run: AgentRun, call: ToolCall, outcome: ToolOutcome, repeated: int = 0
    ) -> None:
        run.tool_calls += 1
        run.suspicious_results += int(outcome.suspicious)
        run.steps.append(
            Step(
                kind="tool",
                text=outcome.text,
                tool=call.name,
                arguments=call.arguments,
                is_error=outcome.is_error,
                suspicious=outcome.suspicious,
                timed_out=outcome.timed_out,
                repeated=repeated,
                seconds=outcome.seconds,
            )
        )

    async def _check(
        self, run: AgentRun, book: SourceBook, messages: list[Message], draft: str
    ) -> Briefing | None:
        """The code's checks on the draft, with one chance to fix uncited sentences, then
        the grounding check."""
        text, links = clean_draft(draft, book)
        missing = missing_citations(text)
        if missing:
            listed = "\n".join(f"- {sentence}" for sentence in missing)
            messages.append(Message.user(RETRY_PROMPT.format(sentences=listed)))
            response = await self._chat(run, messages, None, "retry")
            messages.append(Message.assistant(response.text))
            text, more = clean_draft(response.text, book)
            links += more
        tool_texts = [m.content for m in messages if m.role == "tool"]
        text = drop_unread_citations(text, book, tool_texts)
        if self.verify:
            run.unsupported = await self._verify(run, text, book, tool_texts)
        briefing = finish(text, book, links, run.unsupported)
        if briefing is not None:
            briefing.verified = run.verified
        if briefing is None:
            run.error = "No sentence of the draft passed the checks (source ids, evidence)."
        return briefing

    async def _verify(
        self, run: AgentRun, text: str, book: SourceBook, tool_texts: list[str]
    ) -> list[str]:
        """Ask the model, in a fresh conversation, which sentences the evidence states.
        Only sentences it explicitly calls supported are kept (`unsupported_sentences`)."""
        request, sentences = verification_request(text, book, tool_texts)
        if not sentences:
            return []
        try:
            response = await asyncio.to_thread(
                self.client.chat,
                [Message.user(request)],
                system=self.verify_prompt,
                json_schema=VERIFY_SCHEMA,
                temperature=0,
                # Without thinking, qwen3:8b passed every sentence; with it, it caught
                # exactly the two wrong ones in five briefings (see the learning note).
                think=True,
                # Thinking length varies run to run; the cap keeps one runaway answer
                # (and the client's one retry) inside LLM_TIMEOUT_S.
                max_tokens=VERIFY_MAX_TOKENS,
            )
        except LLMError as error:
            # The briefing is kept, marked as unchecked (`Briefing.verified`).
            logger.warning("The grounding check failed: %s", error)
            run.verified = False
            return []
        run.verified = True
        run.usage = run.usage + response.usage
        rejected = unsupported_sentences(response.data, sentences)
        run.steps.append(
            Step(
                kind="verify",
                text=json.dumps(response.data),
                arguments={"request": request},
                usage=response.usage,
                seconds=round(response.usage.latency_s, 3),
            )
        )
        return rejected
