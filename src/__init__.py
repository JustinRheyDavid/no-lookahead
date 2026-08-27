"""No Lookahead — an LLM reads SEC earnings filings, and a point-in-time
backtest says whether it found anything real.

Import order in this package mirrors the direction data flows: edgar -> pit ->
extract -> evals -> backtest. Nothing imports backwards, and nothing except
``pit`` opens the database. ``tests/test_no_backdoor_reads.py`` enforces the
second half of that once Step 3 lands.
"""
