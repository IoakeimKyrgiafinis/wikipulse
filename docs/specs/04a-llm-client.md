# Spec 04a: LLM client

Status: done
Stage: 4. Knowledge graph
Owner: agent
Depends on: 00

## Goal
One small, provider-agnostic interface for every LLM call, defaulting to a free local model through Ollama, with structured outputs and tool calling.

## What you'll learn
- **How LLM APIs really work:** messages, system prompts, temperature, token usage, tool calling and JSON-schema outputs, by reading the client code closely.
- **Abstraction design:** why the rest of the code never imports Ollama directly, and how that lets you swap models with one setting.
- **Local inference limits:** context windows, GPU memory, model loading time. Watch Task Manager's GPU view during the live tests.

## Context
- Default model `ollama:qwen3:4b` (fast on an 8 GB GPU); `qwen3:8b` is the higher-quality option. Model settings use the format `backend:model`.
- Ollama's default context window is small and silently truncates long prompts, so always pass the context length. Qwen3's "thinking" mode must be switchable per call, off by default.
- An optional `anthropic` backend may exist for later comparison runs; it must never be required.

## Outputs
- `wikipulse/llm/` with neutral types (`Message`, `ToolSpec`, `ToolCall`, `Usage`, `LLMResponse`), `get_client(model_setting)`, and `chat(messages, system, tools, json_schema, temperature, max_tokens, think)`
- An Ollama backend using the `ollama` Python package's native API; an optional, lazily imported Anthropic backend
- `uv run python -m wikipulse.llm.check`: is Ollama reachable, which models are pulled, a one-line test
- Config: `OLLAMA_HOST`, `OLLAMA_NUM_CTX=8192`, `OLLAMA_KEEP_ALIVE=10m`, `LLM_TIMEOUT_S=120`, `DEFAULT_MODEL=ollama:qwen3:4b`

## Requirements
1. With `json_schema`, the result is valid JSON for that schema; retry once on failure, then raise a clear error.
2. Thinking text is stripped from the answer and kept in a separate field.
3. A missing model gives an error suggesting `ollama pull <model>`.
4. Usage (tokens, latency) is always recorded.

## Acceptance criteria
- Unit tests with mocked backends: setting parsing, tool conversion, schema retry, thinking removal, errors.
- Tests marked `live` against local Ollama: plain answer, JSON-schema answer, tool call.

## Look at it yourself (optional, never blocking)
- Run the live tests and note GPU memory use with an 8192-token context.

## Out of scope
- Streaming, fallbacks between models

## Notes for the agent
- Add `AGENTS.md` rule once done: all LLM calls go through `wikipulse.llm`.
