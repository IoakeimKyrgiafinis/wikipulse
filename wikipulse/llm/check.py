"""LLM check: `uv run python -m wikipulse.llm.check [--model backend:model]`.

For Ollama: is the server reachable, which models are pulled, is the chosen model one
of them. Then one short test question, with the answer, tokens and timing. Exits
non-zero, with a hint what to do, if any step fails.
"""

import argparse
import sys

from wikipulse.config import get_settings
from wikipulse.llm.client import LLMClient
from wikipulse.llm.errors import LLMError
from wikipulse.llm.factory import get_client
from wikipulse.logging_setup import setup_logging

TEST_PROMPT = "Reply with exactly one word: pong"


def check_models(client: LLMClient) -> bool:
    """Print the models the backend has; False if the chosen one isn't among them."""
    try:
        models = client.list_models()
    except NotImplementedError:
        return True  # hosted backends: the test call below tells
    print(f"Models pulled: {', '.join(models) or '(none)'}")
    if client.model not in models and f"{client.model}:latest" not in models:
        print(f"Model {client.model!r} is not pulled: run `ollama pull {client.model}`")
        return False
    return True


def test_call(client: LLMClient) -> None:
    """Ask one short question and print the answer with its usage."""
    response = client.chat(TEST_PROMPT)
    usage = response.usage
    print(
        f"Test call: {response.text!r} in {usage.latency_s:.2f} s "
        f"(model load {usage.load_s:.2f} s), {usage.input_tokens} tokens in, "
        f"{usage.output_tokens} out"
    )
    if response.thinking:
        # thinking-only models (such as the qwen3 2507 "thinking" builds) reason even
        # with thinking off; the client moves that text out of the answer
        print(f"  (the model also thought for {len(response.thinking)} characters)")


def main(argv: list[str] | None = None) -> int:
    """Run the check and return a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", help="backend:model to check (default: DEFAULT_MODEL)")
    args = parser.parse_args(argv)
    settings = get_settings()
    setup_logging()
    try:
        with get_client(args.model, settings) as client:
            print(f"Model: {client.setting}")
            if client.backend == "ollama":
                print(f"Ollama: {settings.ollama_host}, context {settings.ollama_num_ctx} tokens")
            if not check_models(client):
                return 1
            test_call(client)
    except (LLMError, ValueError) as error:
        print(f"FAILED: {error}")
        return 1
    print("LLM OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
