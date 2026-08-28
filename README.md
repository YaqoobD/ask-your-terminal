# Ask Your Terminal

Plain-English questions over messy multi-terminal data, built as a repeatable product.

Submission for the **All Your BI Lead AI Engineer** assignment, **Scenario 2**.

**The architecture in one sentence: the LLM picks; it never computes.** It maps language
onto a governed, schema-validated `QueryIntent`. Every number, filter, trust grade and
diagnosis is produced by code.

```
question
  → scope + intent          (answerable? which client?)
  → LLM → QueryIntent JSON  (schema-validated, enum-bound to the metric registry)
  → registry + deterministic SQL compiler
  → warehouse
  → rows + lineage + freshness + completeness
  → graded answer card      (CERTIFIED / QUALIFIED / CLARIFY / REFUSE)
```

## Start here

**[`SOLUTION_BRIEF.html`](SOLUTION_BRIEF.html)** gives the whole solution in two pages:
the architecture diagram, the four assignment questions answered, what is shared across
clients versus configured per client, security, and the rollout. Open it in a browser; it
prints cleanly to two A4 pages.

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
source .venv/bin/activate
```

Set a provider before running anything that calls the model. Create a `.env` file (not
committed, see `.gitignore`):

```bash
export ASK_PROVIDER="claude"
export ANTHROPIC_API_KEY="sk-ant-..."
```

Or, for AWS Bedrock in the EU region instead of the direct API:

```bash
export ASK_PROVIDER="bedrock"
export AWS_REGION="eu-central-1"
export ASK_BEDROCK_INTENT_MODEL="eu.anthropic.claude-opus-5"
export ASK_BEDROCK_NARRATE_MODEL="eu.anthropic.claude-sonnet-5"
```

Then:

```bash
source .env
make seed          # generate the synthetic terminal warehouse
make evals         # run the five-layer eval harness
make demo          # seed if needed, serve, open the browser
```

`make demo` opens a chat-style UI at `http://127.0.0.1:8000` with five example questions,
one per trust grade plus a diagnose case, taken straight from `evals/gold_intents.jsonl`
and `evals/traps.jsonl`.

Runs on any laptop. No cloud account required beyond a model provider. A local open-source
model path via Ollama is included for anyone without API access, documented as
reproducibility mode with expected quality degradation.

## Status

All 8 build phases are complete and merged; phase 9 (deck, clip, rehearsal) is in progress.
Full test suite: `pytest tests`, 103 passed. `make evals`: layers 1 to 4 green.

## Models

| Use | Model | Why |
|---|---|---|
| Intent extraction | Claude Opus 5 | the one unrecoverable step, because a wrong intent produces a confidently wrong number |
| Narration, clarifying questions | Claude Sonnet 5 | bounded rewriting over evidence produced in code |
| Reproducibility fallback | local via Ollama | so this repo runs without a Claude key; degraded, and labelled as such |

## Honest boundaries

- The data is **synthetic**. It proves the logic, not the fit to any real terminal. The
  mess is seeded deliberately: late-arriving corrections, soft-deletes with reversals,
  divergent berth naming per terminal, a timezone offset, a planted dwell-time spike, and
  a prompt-injection string in a remark field.
- The Power BI TMDL sync is parsed from a **sample export**, because Power BI Desktop is
  Windows-only and this was built on macOS. The parser is real; the tenant connection is
  not demonstrated. That is a laptop limitation, not an architectural one.
- Relative time windows ("yesterday", "last month") resolve against real wall-clock time,
  with no pinned-`now` path threaded through the compiler yet. The synthetic dataset's last
  real week is months in the past, so a relative-time question returns an empty window
  today; the demo's example questions use `week N` phrasing instead, which is deterministic.
  Known, not fixed.
- The per-client cost cap mentioned in `SOLUTION_BRIEF.html` is a stated goal, not built:
  cost is logged per answer with real token counts and dollars, capping is a next step.
