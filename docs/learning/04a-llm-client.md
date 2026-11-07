# Learning note: 04a LLM client

## What was built

One small package, `wikipulse/llm/`, through which every LLM call in WikiPulse goes.
Callers write

```python
from wikipulse.llm import get_client

with get_client("ollama:qwen3:4b") as llm:
    response = llm.chat("Is this news?", system=PROMPT, json_schema=SCHEMA)
    response.data, response.text, response.thinking, response.usage
```

and never see which provider answered.

| File | What it does |
|------|--------------|
| `wikipulse/llm/types.py` | The neutral types: `Message`, `ToolSpec`, `ToolCall`, `Usage`, `LLMResponse` (pydantic models). |
| `wikipulse/llm/client.py` | `LLMClient`, the base class with `chat()`: everything that is the same for every provider (timing, usage, removing thinking, checking a JSON answer and retrying once). Also `split_thinking()` and `parse_json()`. |
| `wikipulse/llm/schema.py` | `schema_errors()`: checks a JSON value against a JSON schema (the subset WikiPulse uses). |
| `wikipulse/llm/ollama_backend.py` | `OllamaClient`: local models through the `ollama` package's native chat API. |
| `wikipulse/llm/anthropic_backend.py` | `AnthropicClient`: optional, the `anthropic` package is imported only when used. |
| `wikipulse/llm/factory.py` | `parse_model_setting()` and `get_client()`: the only place that knows which backends exist. |
| `wikipulse/llm/errors.py` | `LLMError` and its subclasses `LLMUnavailableError`, `ModelNotFoundError`, `StructuredOutputError`. |
| `wikipulse/llm/check.py` | `uv run python -m wikipulse.llm.check`: is Ollama reachable, which models are pulled, one test question. |
| `wikipulse/config.py` | `DEFAULT_MODEL`, `OLLAMA_HOST`, `OLLAMA_NUM_CTX`, `OLLAMA_KEEP_ALIVE`, `LLM_TIMEOUT_S`, `ANTHROPIC_API_KEY` (all in `.env.example`). |
| `tests/llm/` | 47 unit tests (a scripted fake backend, Ollama behind a mocked HTTP transport, a fake Anthropic SDK) and 3 `live` tests against the local Ollama. |

How a call flows:

```
caller ──chat(messages, system, tools, json_schema, temperature, max_tokens, think)──►
  LLMClient.chat()            (client.py, same for every provider)
    ├─ a plain string becomes [Message.user(...)]
    ├─ _ask(): time the call ─► backend._send(ChatRequest)
    │                              OllamaClient: neutral → Ollama dicts → ollama.Client.chat()
    │                              AnthropicClient: neutral → Anthropic blocks → messages.create()
    │          ◄─ LLMResponse (text, thinking, tool calls, tokens, stop reason)
    │    split_thinking(): move <think>...</think> out of the text; add up usage
    ├─ json_schema given? parse_json() + schema_errors()
    │    invalid → one retry, with the bad answer and the error fed back
    │    still invalid → StructuredOutputError
    └─ return LLMResponse
```

The two LLM labelers from stage 2 and 3 (`wikipulse/eval/label.py` and
`wikipulse/eval/grouping_labels.py`) used to call Ollama's REST API themselves with
httpx. They now use the client (`LLMLabeler`, `LLMPairLabeler`), so there is no LLM call
left outside `wikipulse.llm`. `AGENTS.md` has the new rule.

## Key concepts

### How LLM APIs really work

Read `wikipulse/llm/types.py` and `ollama_backend.py` together; every concept of a
chat API shows up as a field:

- **Messages.** A conversation is a list of turns with a role. `user` is what we ask,
  `assistant` is what the model answered earlier (the model has no memory: to continue
  a conversation you send the whole history again), `tool` is the result of a tool the
  model asked for. `Message.user()`, `Message.assistant()`, `Message.tool_result()`
  build them.
- **System prompt.** Instructions that frame the whole conversation ("you judge bursts
  of editing ..."). Ollama takes it as a first message with role `system`
  (`ollama_messages()`); Anthropic takes it as a separate `system` parameter
  (`anthropic_arguments()`). That difference is exactly why `chat()` takes `system`
  separately and lets each backend place it.
- **Temperature.** How random the sampling of the next token is. `chat()` defaults to
  0 (always the likeliest token, as repeatable as possible), which is what labelers
  and extraction want; `temperature=None` leaves the model's own default (0.6 for
  qwen3).
- **Token usage.** Every answer reports how many tokens the model read
  (`prompt_eval_count` in Ollama, `usage.input_tokens` in Anthropic) and wrote
  (`eval_count` / `output_tokens`). `from_ollama_response()` copies them into `Usage`,
  `LLMClient._ask()` adds the wall-clock latency, and `client.total_usage` adds up a
  whole run. Every call is logged at DEBUG level.
- **Tool calling.** We describe tools (`ToolSpec`: name, description, a JSON schema of
  the arguments); the model may answer with `tool_calls` instead of text. *The model
  never runs anything*: our code runs the tool, appends the assistant turn with its
  calls and a `tool` message with the result, and calls `chat()` again.
  `tests/llm/test_live.py::test_tool_call` does exactly this round trip with an `add`
  tool. Providers disagree on the format: Ollama uses OpenAI-style `{"type":
  "function", "function": {...}}` tools and matches results by tool name (it gives
  calls no id, so `from_ollama_response()` numbers them `call_0`, `call_1`, ...);
  Anthropic uses `tool_use` blocks in the assistant turn and `tool_result` blocks in a
  *user* turn, matched by id (`anthropic_messages()`).
- **JSON-schema outputs.** With `json_schema`, Ollama's `format` parameter constrains
  decoding: at each step only tokens that keep the output valid for the schema are
  allowed. That makes a valid answer very likely but not certain (the model can still
  stop early at `max_tokens`), so `chat()` always checks it (`parse_json()`,
  `schema_errors()`), and on failure asks again *with the error in the conversation*
  (`RETRY_PROMPT`). The Anthropic backend has no constrained decoding here, so the
  schema goes into the system prompt and the same check catches mistakes.
- **Thinking.** Reasoning models write their reasoning before the answer, between
  `<think>` and `</think>`. `split_thinking()` moves it to `response.thinking`, so
  callers never parse it by accident. `think=False` is the default (faster, cheaper).

### Abstraction design

Only `ollama_backend.py` imports `ollama`, only `anthropic_backend.py` imports
`anthropic`, and only `factory.py` knows the list of backends. Everything else sees the
neutral types. The benefits:

- **Swap models with one setting.** `DEFAULT_MODEL=ollama:qwen3:8b`, or
  `EVAL_LABEL_MODEL=anthropic:claude-...` for a comparison run, without touching code.
  The setting format is `backend:model`; `parse_model_setting()` splits at the first
  colon only, because Ollama names contain colons themselves (`qwen3:4b`).
- **Shared logic is written once.** Retry, thinking removal and usage live in
  `LLMClient.chat()`; a backend only implements `_send()` (convert, call, convert
  back). Adding a provider is one new file with one method.
- **Testing without a model.** `tests/llm/helpers.py::ScriptedClient` is a backend that
  answers from a list, which is how the retry and thinking logic is tested. The Ollama
  backend is tested through a mocked HTTP transport (the real `ollama` package runs,
  only the network is fake), the Anthropic backend with a fake SDK object.
- **Errors mean the same everywhere.** A missing model is `ModelNotFoundError` from any
  backend ("run `ollama pull qwen3:8b`" for Ollama), a server that is down is
  `LLMUnavailableError`.

### Local inference limits

Measured on this machine (RTX 5060 Laptop GPU, 8 GB):

| Context (`OLLAMA_NUM_CTX`) | Size in memory (`ollama ps`) | Where it runs | Test call (`wikipulse.llm.check`) |
|---:|---:|---|---|
| 8192 (default) | 3.9 GB (nvidia-smi: 3.9 of 8.0 GB used) | 100% GPU | 2.9 s, of which 2.0 s loading on the first run |
| 32768 | 7.9 GB (nvidia-smi: 6.1 GB used) | 22% CPU / 78% GPU | 9.8 s, of which 4.5 s loading |

- **Context windows cost memory.** The model weights of qwen3:4b take about 2.5 GB; the
  rest is the KV cache, which grows with the context length. At 32k tokens it no longer
  fits on the GPU and a fifth of the model runs on the CPU, which is much slower. That
  is why the context length is a setting and why it is always sent: Ollama's default
  is small and **silently cuts off** longer prompts.
- **Changing `num_ctx` reloads the model** (the 8192 → 32768 switch above cost 4.5 s
  of loading). Keep one value for a whole run.
- **Loading time.** The first call after Ollama unloaded the model pays seconds of
  loading; `Usage.load_s` shows it. `OLLAMA_KEEP_ALIVE=10m` keeps the model in memory
  between calls, so a labeling run pays it once.

## Design decisions and deviations from the spec

- **The labelers moved onto the client** (within the spec's goal "every LLM call").
  `OllamaLabeler` became `LLMLabeler(client)` and `OllamaPairLabeler` became
  `LLMPairLabeler(client)`; their own retry loops and 404 handling are gone because the
  client does both. The first request of each label is the same as before (same
  prompt, schema, `think: false`, temperature 0, `num_ctx` 8192), so `PROMPT_VERSION`
  stays; only a retry now carries the error message. Their timeout went from a
  hard-coded 180 s to `LLM_TIMEOUT_S` (120 s). The tests of both labelers now build an
  `OllamaClient` on a mocked transport; their mocked replies gained the `role` field,
  which the `ollama` package requires (real Ollama always sends it). `test_model_setting`
  used `anthropic:claude` as an example of an *invalid* setting; since `anthropic` is a
  backend now, it uses `openai:gpt-4o` instead.
- **Module names `ollama_backend.py` / `anthropic_backend.py`**, not `ollama.py`, so a
  reader never confuses our module with the `ollama` package; `get_client()` lives in
  `factory.py` (the backends import `client.py`, so it can't live there).
- **Own JSON-schema check instead of the `jsonschema` package** (no new dependency):
  `schema.py` covers `type`, `enum`, `const`, `properties`, `required`,
  `additionalProperties`, `items`, length and number bounds and `anyOf`, which is all
  WikiPulse's schemas use. Unknown keywords are ignored, so the check can only be too
  lenient, never reject a valid answer.
- **The retry tells the model what was wrong** (the error and the schema). Sending the
  identical request again at temperature 0 would most likely give the same broken
  answer.
- **No retry when the model calls a tool**, even with `json_schema`: a tool call is a
  valid answer, the JSON comes after the tool result.
- **`temperature` defaults to 0** (the spec gives no default); `None` means the model's
  default.
- **`Usage` also has `load_s` and `calls`**: model loading is part of the "local
  inference limits" lesson, and `calls` shows when a retry happened (usage is summed
  over both requests).
- **Anthropic is an optional extra** in `pyproject.toml` (`uv sync --extra anthropic`),
  not a dependency; `uv sync` doesn't install it. Without it, `get_client("anthropic:...")`
  raises a clear `LLMError`. Structured output goes through the system prompt, and
  `think=True` uses a 2048-token thinking budget (Anthropic then forbids setting the
  temperature). It is unit-tested with a fake SDK only; no real Anthropic call was made.
  `ANTHROPIC_API_KEY` is a setting (a `SecretStr`), because pydantic-settings reads
  `.env` into the settings, not into the environment where the SDK would look.
- **`OLLAMA_HOST` moved** from the evaluation section of `config.py` and `.env.example`
  to the new LLM section (same name and default).
- **The `live` marker** now reads "touches real outside services (Wikimedia over the
  network, or the local Ollama)", since the spec puts the Ollama tests under `live`.
- **The installed `qwen3:4b` is a thinking-only build.** `ollama show qwen3:4b` reports a
  262144-token context, i.e. the 2507 "thinking" release, not the original hybrid Qwen3.
  With `think: false` it still reasons, in plain text, and ends the reasoning with a
  lone `</think>` before the answer ("Hmm, the user just asked ... </think>\n\npong").
  `split_thinking()` handles exactly that case (a `</think>` without `<think>`), so
  `response.text` is still just "pong", but thinking off doesn't save the time here
  (198 output tokens for one word). With a JSON schema the constrained decoding allows
  no free text, so the labelers get no reasoning and stay fast (about 3 s per pair).
  The spec's default model `ollama:qwen3:4b` is the one installed; `qwen3:8b`
  (`EVAL_LABEL_MODEL`'s default) is still not pulled and was not pulled for this spec.
- **Out of scope, not built:** streaming, falling back from one model to another.

## Results

- Unit tests: 47 in `tests/llm/` (setting parsing, tool conversion both ways for both
  backends, schema retry and final error, thinking removal, missing model, unreachable
  server, timeout, usage), all passing; the whole suite passes (319 unit, 20
  integration).
- Live tests (`uv run pytest -m live tests/llm`): plain answer, JSON-schema answer and a
  two-step tool call against `ollama:qwen3:4b` all pass, about 12 s for the three.
- `uv run python -m wikipulse.llm.check`: `'pong' in 2.93 s (model load 0.01 s), 17
  tokens in, 198 out`, the model also thought for 814 characters. With
  `--model ollama:qwen3:8b` it lists the pulled models and says
  "run `ollama pull qwen3:8b`"; with Ollama unreachable it says how to start it.
- The migrated pair labeler, called on a real pair (an Ohio election and darts):
  `different`, 493 tokens in, 53 out, 3.0 s.

## Questions to test your understanding

1. Why does `chat()` take the system prompt as its own parameter instead of as a
   `Message` with role `system`?
2. Ollama constrains the output with the JSON schema. Why does `chat()` still check the
   answer, and why does the retry add the error to the conversation instead of sending
   the same request again?
3. You raise `OLLAMA_NUM_CTX` from 8192 to 32768 so longer pages fit into one prompt.
   What happens on this 8 GB GPU, and what would you notice in `Usage`?

<details>
<summary>Answers</summary>

1. Providers treat it differently: Ollama expects it as the first message, Anthropic as
   a separate `system` parameter (and allows only `user` and `assistant` roles in the
   messages). Keeping it separate lets each backend put it where its API wants it
   (`ollama_messages()`, `anthropic_arguments()`), and the Anthropic backend can add the
   schema instruction to it.
2. Constrained decoding makes valid JSON very likely, but an answer can still be cut off
   at `max_tokens`, and the Anthropic backend has no constraint at all; callers rely on
   `response.data` matching the schema, so it is checked in one place for every
   backend. At temperature 0 the identical request would most likely produce the
   identical broken answer; showing the model its answer and what was wrong
   (`RETRY_PROMPT`) changes the input, so the second answer can differ.
3. The KV cache grows with the context: the model's memory went from 3.9 GB to 7.9 GB,
   which no longer fits on the GPU, so Ollama runs 22% of it on the CPU. Every call gets
   slower (higher `usage.latency_s`), and the first call after the change reloads the
   model (high `usage.load_s`, 4.5 s here). Larger prompts also raise
   `usage.input_tokens` and the time to read them.

</details>
