---
name: ship-all
description: Ship every remaining spec in order, fully automatically. Only use when the user invokes /ship-all.
disable-model-invocation: true
argument-hint: "[optional: last spec to ship, e.g. 05e-api-and-web]"
---

Ship all remaining specs automatically, in the order of the Specs table in `docs/ROADMAP.md`.

1. Read `AGENTS.md` and `docs/ROADMAP.md`.
2. Check prerequisites once: Docker is running (`docker info`) and Ollama is reachable (`ollama list`). If either fails, stop and tell the human what to start.
3. For each spec in table order whose file doesn't say `Status: done`:
   - Follow every step of `.claude/skills/ship-spec/SKILL.md` for that spec.
   - If it ends `blocked`, stop the whole run and report which spec and why.
   - After each spec, update its row in the ROADMAP table to `done` and print one line: spec name, verdict, learning note path.
   - If `$ARGUMENTS` names a spec, stop after shipping it.
4. Keep your own context small: pass file paths to subagents rather than pasting content, and don't re-read finished work.
5. When finished (or stopped), print a summary table of every spec shipped in this run, and the next spec to ship.

This run can be resumed at any time: running `/ship-all` again continues from the first spec that isn't done.
