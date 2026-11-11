"""The documented Claude Desktop config must be valid JSON that starts our server."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "docs" / "claude_desktop_config.example.json"
README = ROOT / "docs" / "usage.md"  # the command reference, moved out of README.md in spec 10


def _check_config(config: dict) -> None:
    server = config["mcpServers"]["wikipulse"]
    assert server["command"] == "uv"
    args = server["args"]
    assert args[args.index("--directory") + 1] == "C:\\path\\to\\wikipulse"
    assert args[-2:] == ["-m", "wikipulse.mcp_server.server"]


def test_the_example_config_is_valid_json() -> None:
    _check_config(json.loads(EXAMPLE.read_text(encoding="utf-8")))


def test_the_readme_config_snippet_is_valid_json() -> None:
    text = README.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, flags=re.DOTALL)
    snippets = [block for block in blocks if '"mcpServers"' in block]
    assert len(snippets) == 1
    _check_config(json.loads(snippets[0]))
