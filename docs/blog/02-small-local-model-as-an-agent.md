# Making an 8B local model a usable agent

*Draft. Built from the learning notes of specs 04a, 04b, 05a, 05b and 05d. Everything
ran on a laptop GPU with 8 GB, through Ollama.*

WikiPulse has an analyst agent: for each significant news event it investigates through
tools (the knowledge graph, the Wikipedia pages behind it) and writes a short briefing
in which every sentence cites the Wikipedia revision it comes from. It runs on qwen3:8b,
locally, for free. A frontier model would make this easy. This post is about what it took
to make a small one trustworthy, and the short version is: put the control in code, and
treat everything the model sees as an example it will copy.

## The loop, without a framework

An agent is a language model in a loop with tools. `AnalystAgent._loop()` is about forty
lines: send the conversation and the tool list, run any tool calls the model asks for,
append the results, repeat until the model answers in plain text. The model never runs
anything; it only asks. Writing the loop by hand made every control visible:

| Control | What happens when it trips |
|---|---|
| 8 model turns with tools | stop investigating |
| 60,000 tokens over all calls | stop investigating |
| 900 s wall clock | stop investigating |
| the context window 85% full | stop before Ollama silently cuts the prompt |
| the same call twice | the second isn't run; the model gets the earlier result and "you already called this"; a third stops the run |
| a tool slower than 30 s (`ask`: 180 s) | the model reads "did not answer ... do not call it again" |

Whatever stops the investigation, the model gets one last turn *without tools* to write
the best briefing the facts allow, and the transcript records a named stop reason. A
failing tool never ends a run; it becomes a message the model can act on.

The agent is also a real MCP client: it lists the tools from WikiPulse's MCP server and
sees exactly what Claude Desktop sees. Its first tool call, `get_event`, is made by the
code and put into the conversation as if the model had asked, which saves a turn and
shows a small model what a tool call and a result look like.

## A small model copies what is in front of it

The transcripts of the first runs were full of errors, and almost none were fixed by
"better instructions". They were fixed by changing what the model *sees*:

- **Data format.** Relations were shown as `Amy Acton | defeated | Vivek Ramaswamy`, and
  a briefing reversed them. Rendering them as plain sentences ("Amy Acton defeated Vivek
  Ramaswamy. [F3]") fixed it.
- **Examples.** A briefing said "Ueno Bank collapsed in October", a date in no data. It
  was the prompt's example sentence. The example now uses an invented council.
- **Ids.** The model called `read_page` with the title "2026 SAFF Championship [F2]",
  copying a source id. The tool now strips source markers from titles: accept what
  models type.
- **Procedure over goals.** "Investigate the event" produced briefings written from the
  first tool result plus invention ("followed public pressure"). "Call `read_page` for the
  first two pages" was followed.
- **Thinking, measured.** With thinking on, the agent called no tool at all on five of
  five events and wrote more errors. Thinking stays off for the investigation.

Tool results are rendered compactly too: a raw `get_event` was up to 11.7 KB of JSON; the
rendered version, with each source replaced by a short id like `[F4]`, is about 600
tokens. The code, not the model, knows that `[F4]` is "Santiago Peña, revision
1383977729", writes the source list, and removes any id the run never read.

## Checking the model's work with the model

Two errors survived all of that: a licence year off by one, and "this ended the
California death penalty moratorium" where the data says the opposite. A second model
call, a grounding check that compares each sentence with the evidence it cites, caught
both, but only with thinking on. Without thinking it passed all 17 sentences of the five
briefings. With thinking it sometimes reasoned for over 3,000 tokens, so it got a token
cap, and a check that fails keeps the briefing marked "could not be checked" instead of
losing it.

The review then found that the published sentences had been checked against the event's
data as a whole, not against the page each sentence cites. Several were true but not
stated by their cited source. Checking citations precisely is harder than checking
facts, for models and for the people evaluating them.

## Prompt injection: defenses in code first

The agent reads Wikipedia, which anyone can edit. The defense is in layers, most of them
not in the prompt: every tool is read-only and the only write goes to the event the code
chose; sentences that address an AI ("ignore all previous instructions", "AI summarizers
should ...") are withheld before the model sees them; links are stripped on the way in
and on the way out; page text is fenced as untrusted; and every sentence needs a valid
source. Nine invented attack cases run through the real agent: 8 of 9 passed before the
withholding defense (the model repeated a planted "hoax" claim), 9 of 9 after.

## What generalizes

The same pattern held everywhere a small model was used in WikiPulse:

- **Constrain the output, not just the prompt.** Extraction uses a JSON schema built per
  event (entity references are an `enum` of the ids that exist); the 4B model follows the
  schema closely and prose rules loosely. A second prompt asking for fewer, verb-phrase
  relations changed nothing beyond the noise.
- **Check generated code before running it.** The GraphRAG answerer lets the LLM write one
  read-only Cypher query, which is validated first. The evaluation showed both sides of
  that: four answerable questions were refused because the generated query never ran
  (three syntax errors and one false positive of the validator).
- **Drop what isn't cited.** Uncited sentences are removed by code; faithfulness on the
  25-question QA eval is 95.9%. The remaining errors are in choosing, counting and
  refusing, not inventing.
- **Measure the judge.** The QA judge (qwen3:8b) and a second judge (qwen3:4b) agree on
  correctness with a linear-weighted kappa of 0.82, yet a hand reading of the extraction
  judge's verdicts suggests its 93.9% precision is closer to 80%. An 8B judge is good at
  "does the source mention this?" and weak at "is the subject really the one doing it?".

A small local model can be a usable agent. It just needs the things a careful junior
colleague needs: a concrete procedure, clean inputs without misleading examples, limits,
and someone checking the work.
