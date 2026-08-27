# No Lookahead

An LLM reads SEC earnings filings. A point-in-time backtest says whether it
found anything real.

**Status: in progress.** Step 1 of 13. The build plan is
[`docs/plans/no-lookahead.md`](docs/plans/no-lookahead.md); the numbers below
land as the steps that produce them do.

---

## The result

<!-- Step 12 fills this in. It goes here, above the architecture, because it is
     the point of the project. If the signal turns out to be noise, that is
     what this table will say. -->

| Version of the same strategy | Sharpe | t-stat |
|---|---:|---:|
| As a typical portfolio project would report it | — | — |
| — minus survivorship bias | — | — |
| — minus lookahead on filing dates | — | — |
| — minus restated fundamentals | — | — |
| — with realistic transaction costs | — | — |
| Pre-registered, sealed holdout, one run | — | — |

## What this is

Roughly six thousand 8-K earnings releases are read by a language model running
locally, which extracts a structured record from each: revenue and EPS with
their GAAP or non-GAAP basis, the direction of any guidance change, and a short
list of notable items. Those extractions are then tested — properly — against
what the stock actually did.

Two things make it different from the usual version of this project.

**The evaluation is not vibes.** Extraction accuracy is scored automatically
against XBRL: the press release states a quarter's revenue, and the 10-Q filed
weeks later tags the same figure, so several thousand items are labelled for
free. A further three hundred filings are labelled by hand for the judgements
XBRL cannot settle, and the LLM judge used on the free-text fields is itself
validated against those human labels before any number it produces is believed.

**Lookahead is prevented by construction, not by discipline.** Every fact in
the store carries `available_at` — the moment it became publicly knowable — and
the only way to read is `as_of(t)`, which cannot return anything later. The
prompt builder reads through the same interface, so an extraction is
structurally incapable of seeing the future it is being asked to predict. Two
tests defend it, and they were written before any signal existed.

## Cost

Zero. No paid API, no paid data, no cloud bill. EDGAR, XBRL, daily prices and
the Fama-French factors are all free; inference runs locally on an M4. What the
project spends instead is wall-clock time, which is measured and reported in
[`reports/throughput.md`](reports/) rather than waved at.

## Running it

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
export EDGAR_USER_AGENT="Your Name you@example.com"   # the SEC requires this
.venv/bin/pytest -q -m "not network"
```

The `EDGAR_USER_AGENT` is not optional and there is no default: the SEC refuses
requests that do not identify the sender, so the client raises at construction
rather than failing forty minutes into a backfill.

## Limitations

<!-- Populated as they are measured, led by the residual survivorship gap from
     Step 4. Anything discovered and not yet fixed belongs here, not omitted. -->

## Layout

| Path | Holds |
|---|---|
| `src/edgar.py` | Every request to sec.gov. Rate limited, cached forever |
| `src/pit.py` | The point-in-time store and its one read path |
| `src/extract.py` | Prompts, schema, and the local inference loop |
| `src/evals/` | The three scoring tiers and the judge's own validation |
| `src/backtest.py` | Event study, portfolio simulation, costs |
| `src/biases.py` | Reintroducing each bias on purpose, to price it |
| `data/gold/labels.jsonl` | Three hundred hand labels. Not regenerable |
| `data/registry.jsonl` | Every experiment ever run — the trial count that deflates the Sharpe |
