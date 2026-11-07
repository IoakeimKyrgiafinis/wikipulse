# Spec 05d: Analyst agent

Status: ready
Stage: 5. GraphRAG, MCP, agent
Owner: agent
Depends on: 05c

## Goal
An agent that investigates each significant new event through the MCP tools plus a few local ones, and writes a short, cited briefing.

## What you'll learn
- **The agent loop:** model, tool call, result, repeat. Every agent framework is built on this. Read `loop.py` closely; it's deliberately written without a framework.
- **Agent orchestration and control:** budgets, loop detection, timeouts, and stopping well.
- **Prompt injection in practice:** Wikipedia text is written by anyone, so your agent reads untrusted text by design.
- **Small-model agents:** what breaks with a 4B or 8B model, and which prompting and tool-design changes fix it.

## Outputs
- `wikipulse/agent/loop.py`: written without a framework. Connects to the MCP server as a client, gives the tools to the model through `wikipulse.llm`, and runs the loop until a final briefing or `AGENT_MAX_STEPS`
- `wikipulse/agent/prompts/analyst_v1.md`
- Briefings saved on the `Event` node, with citations; transcripts saved to `data/agent_runs/`

## Requirements
1. Controls: a token budget, loop detection (same tool and arguments three times), per-tool timeouts, and a final no-tools answer when limits are hit.
2. A trigger runs the agent for events above a configurable strength.
3. Prompt-injection tests: replay events whose page text contains planted instructions (using example.com URLs) and check the briefing doesn't follow them.
4. Tests with a scripted fake LLM.

## Acceptance criteria
- Briefings for five real events are accurate and cited.
- All injection tests pass, or failures are documented with a defense added.

## Design notes
- Optional extension: rebuild the same agent with LangGraph afterwards and compare the code, control and debugging experience. Good interview material.
