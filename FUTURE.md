# Deliberately not in v1

Ideas that came up during the build and were kept out of it. Each is here
because it is genuinely interesting and would have doubled the scope.

- **10-K Item 1A risk-factor diffs.** The year-over-year change in risk
  language has real literature behind it. It is a second corpus, a second
  schema, and a second eval tier.
- **A frontier-model reference tier.** `src/llm.py` has the adapter seam for
  it. Running a few hundred filings through a hosted model would price the gap
  between local and frontier extraction — but the project's cost constraint is
  zero, and the three hundred hand labels are a better quality ceiling anyway.
- **Earnings call transcripts.** Strictly better text than press releases and
  not freely licensable in bulk. Revisit only with a legitimate source.
- **Intraday execution.** Would let the event study measure the reaction in the
  minutes after acceptance rather than to the next open. Needs paid data.
- **Consensus estimates.** Real surprise instead of the seasonal-random-walk
  proxy. Also paid.
