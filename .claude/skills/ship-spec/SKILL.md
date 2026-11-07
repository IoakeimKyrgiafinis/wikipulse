---
name: ship-spec
description: Fully automated implement, review, fix and merge for one spec. Only use when the user invokes /ship-spec.
disable-model-invocation: true
argument-hint: <spec name, e.g. 01-stream-consumer>
---

Ship the spec `$ARGUMENTS` from `docs/specs/` (add `.md` if it was omitted), fully automatically. You are the coordinator: delegate implementation and review to subagents so each starts with a clean context, and don't write code yourself.

1. **Checks.** Read `AGENTS.md` and the spec. If a spec listed under `Depends on:` isn't `Status: done`, stop and say which. If git has uncommitted changes, commit them as "WIP before <spec>" first. Switch to `main`, then create and switch to the branch `spec-<spec name>`.
2. **Implement.** Use the `spec-implementer` subagent with the spec path. If it reports the spec is impossible, set the spec's status to `blocked` with the reason and stop.
3. **Review once.** Use the `spec-reviewer` subagent. It writes `docs/reviews/<spec name>.md`.
4. **Fix once, if needed.** If the verdict is `fix` or `redo`, use a NEW `spec-implementer` subagent to fix every `high` and `medium` problem, passing the spec path, the report path and the problem numbers. Then run the full test suite and ruff yourself. There is no second review.
5. **Merge.** If tests and ruff pass, merge the branch into `main`, set the spec's status to `done`, commit, and push if a remote named `origin` exists. If they still fail, set the status to `blocked` with the failing checks, leave the branch unmerged, and stop.
6. **Report** in a few lines: what was built, the review verdict, problems fixed, remaining `low` problems, and the path of the learning note.
