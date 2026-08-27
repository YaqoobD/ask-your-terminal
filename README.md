# Ask Your Terminal

Plain-English questions over messy multi-terminal data, built as a repeatable product.

Submission for the **All Your BI — Lead AI Engineer** assignment, **Scenario 2**.

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

**[`DECISIONS.html`](DECISIONS.html)** — the 16 architecture decisions, each with the
reasoning, the alternatives rejected and why, and the strongest objection to each.
Open it in a browser.

## Running it

*Not yet buildable — the build is in progress. This section is filled in as phases land.*

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
make seed          # generate the synthetic terminal warehouse
make evals         # run the five-layer eval harness
make demo          # seed, serve, open the answer card
```

Runs on any laptop. No cloud account required. A local open-source model path is included
for anyone without Claude API access, documented as reproducibility mode with expected
quality degradation.

## Status

| Phase | | |
|---|---|---|
| 1 | Scaffold + synthetic terminal data | not started |
| 2 | Metric registry + TMDL parser | not started |
| 3 | Intent schema + SQL compiler | not started |
| 4 | Provider layer + intent extraction | not started |
| 5 | Diagnose engine | not started |
| 6 | Grading, refusal, cache, telemetry | not started |
| 7 | Eval harness | not started |
| 8 | API + answer-card UI | not started |
| 9 | Docs + rehearsal | not started |

## Models

| Use | Model | Why |
|---|---|---|
| Intent extraction | Claude Opus 5 | the one unrecoverable step — a wrong intent produces a confidently wrong number |
| Narration, clarifying questions | Claude Sonnet 5 | bounded rewriting over evidence produced in code |
| Reproducibility fallback | local via Ollama | so this repo runs without a Claude key; degraded, and labelled as such |

## Honest boundaries

- The data is **synthetic**. It proves the logic, not the fit to any real terminal.
  The mess is seeded deliberately — late-arriving corrections, soft-deletes with
  reversals, divergent berth naming per terminal, a timezone offset, a planted dwell-time
  spike, and a prompt-injection string in a remark field.
- The Power BI TMDL sync is parsed from a **sample export**, because Power BI Desktop is
  Windows-only and this was built on macOS. The parser is real; the tenant connection is
  not demonstrated. A laptop limitation, not an architectural one.
