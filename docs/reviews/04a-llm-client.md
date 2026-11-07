# Review: 04a-llm-client

Branch `spec-04a-llm-client` (one commit, 7118728) against `main`.

## Acceptance criteria

| Criterion | Result | Evidence |
|-----------|--------|----------|
| Outputs: `wikipulse/llm/` with `Message`, `ToolSpec`, `ToolCall`, `Usage`, `LLMResponse`, `get_client(model_setting)`, `chat(messages, system, tools, json_schema, temperature, max_tokens, think)` | pass | `wikipulse/llm/types.py`, `factory.py`, `client.py`. The `chat()` signature matches the spec and everything is exported from `wikipulse/llm/__init__.py`. |
| Ollama backend uses the `ollama` package's native API; Anthropic backend is optional and imported lazily | pass | `ollama_backend.py` calls `ollama.Client.chat`. `anthropic` is imported only inside `make_sdk_client()` and is an optional extra in `pyproject.toml`. `test_the_backend_is_optional_and_needs_a_key` passes without the package. |
| `uv run python -m wikipulse.llm.check` | pass | Default model: "Models pulled: qwen3:4b … Test call: 'pong' in 2.07 s … LLM OK.", exit 0. With `--model ollama:qwen3:8b`: "Model 'qwen3:8b' is not pulled: run `ollama pull qwen3:8b`", exit 1. |
| Config: `OLLAMA_HOST`, `OLLAMA_NUM_CTX=8192`, `OLLAMA_KEEP_ALIVE=10m`, `LLM_TIMEOUT_S=120`, `DEFAULT_MODEL=ollama:qwen3:4b` | pass | `wikipulse/config.py` and `.env.example`. The defaults are covered by `test_llm_defaults`. |
| Req 1: structured answer, one retry, then a clear error | pass | `LLMClient.chat`, tested by `test_an_invalid_structured_answer_is_retried_once_with_the_error` and `test_a_second_invalid_answer_raises_a_clear_error`. The live `test_json_schema_answer` passes. |
| Req 2: thinking stripped and kept separately | pass | `split_thinking` plus `message.thinking`. Unit tests are parametrized. In the live check, the qwen3:4b build here is thinking-only (`/api/show` reports thinking default true) and puts its reasoning plus `</think>` in `content` even with `think:false`. The client moved 772 characters of it out of the answer. |
| Req 3: missing model suggests `ollama pull <model>` | pass | `test_a_missing_model_suggests_pulling_it` and `test_check_tells_how_to_pull_a_missing_model`. Confirmed live with the check (see above). |
| Req 4: usage (tokens, latency) always recorded | pass | `_ask()` sets latency and calls, and accumulates `total_usage`. A retry sums the usage. Tests: `test_usage_is_recorded_per_call_and_in_total`, `test_response_text_thinking_and_usage`. |
| AC: unit tests with mocked backends (setting parsing, tool conversion, schema retry, thinking removal, errors) | pass | `uv run pytest`: 319 passed, 23 deselected. Covered in `tests/llm/test_client.py`, `test_ollama_backend.py` and `test_anthropic_backend.py`. No network access: an httpx MockTransport and a fake SDK stand in. |
| AC: live tests against local Ollama (plain, JSON-schema, tool call) | pass | `uv run pytest -m live tests/llm -v`: 3 passed in 11.1 s. |
| Ruff | pass | `ruff check .`: all checks passed. `ruff format --check .`: 171 files already formatted. |
| AGENTS.md rule "all LLM calls go through `wikipulse.llm`" | pass | Added under Rules (the spec asks for this). `git grep` finds no `import ollama`/`anthropic` or `/api/chat` outside `wikipulse/llm/`. |
| No real email in committed files | pass | `git grep "gmail\|makiskirgia" HEAD` finds no matches. |
| Labelers (02b/03b) behave the same | mostly | The existing labeler tests pass unchanged in substance: request body (schema, think false, num_ctx), one retry, `ollama pull` message, skip logic. There are documented changes: the retry now sends the error back to the model, and the timeout went from 180 s to `LLM_TIMEOUT_S` (120 s). One regression is listed as problem 1. |

## Problems

1. **medium**. `wikipulse/eval/label.py:213` and `wikipulse/eval/grouping_labels.py:209` (`LLMLabeler.label` / `LLMPairLabeler.label`, `return parse_…answer(response.text)`). The labelers re-parse `response.text` with `json.loads` and ignore the already validated `response.data`. The client's `parse_json` accepts JSON in a code fence and does not retry it, so a ```` ```json … ``` ```` answer passes the client's check and then fails in the labeler with `JSONDecodeError` and no retry. Before this change the labeler retried that case. I reproduced this with a `ScriptedClient`. It is unlikely with Ollama's constrained `format`, but likely with the anthropic backend, which only asks for the schema in the prompt.
2. **low**. `wikipulse/llm/client.py:771-787` (`split_thinking`). For a thinking-only model like the installed qwen3:4b, an answer cut off by `max_tokens` before `</think>` has no tag at all. The whole reasoning text is then returned as `response.text` and `thinking=None`, so the docstring's claim that "an unclosed `<think>` is all thinking" does not cover the case that actually happens with this model.
3. **low**. `wikipulse/llm/ollama_backend.py` (`from_ollama_response`). Tool call ids are `call_0`, `call_1`, … and restart with every response. In a multi-round tool loop, two different calls share an id, so `Message.tool_result` / `tool_call_id` cannot tell them apart. This works today only because Ollama matches results by `tool_name`.
4. **low**. `wikipulse/llm/ollama_backend.py` (`OllamaClient.__init__`) and `wikipulse/llm/anthropic_backend.py` (`AnthropicClient.__init__`, `DEFAULT_MAX_TOKENS = 4096`, `THINKING_BUDGET = 2048`). The constructor defaults repeat the config defaults (`"http://localhost:11434"`, `8192`, `"10m"`, `120.0`), so the two can drift apart. The Anthropic token budgets are tunables kept outside `config.py`.
5. **low**. `wikipulse/eval/label.py` (`LLMLabeler`). The old 404 message also said "or choose another with EVAL_LABEL_MODEL". The generic `ModelNotFoundError` text no longer names the setting the user should change.
6. **low**. `wikipulse/llm/client.py` (`ChatRequest`/`chat`). Usage of a failed structured call (both attempts) only reaches `total_usage` and the debug log. `StructuredOutputError` carries `last_text` but no `usage`, so a caller that catches it cannot attribute the cost.

Verdict: fix

## Questions for the human before merging

1. Your installed `qwen3:4b` spent about 180 output tokens thinking before answering "pong", even though `think=False` was sent. Why does that happen? Which function keeps that reasoning out of `response.text`, and what does it cost you in latency and context when labeling hundreds of spikes?
2. A structured answer is checked twice: Ollama constrains decoding with `format=json_schema`, and then `wikipulse/llm/schema.py` validates the result again. Why isn't the constraint enough on its own? Which situations does each check catch, for Ollama and for the anthropic backend?
3. If you switch `EVAL_LABEL_MODEL` to `anthropic:<model>`, which code paths change: system prompt placement, schema handling, thinking, temperature, and errors? Given problem 1, what would happen the first time Claude wraps its JSON answer in a code fence?
