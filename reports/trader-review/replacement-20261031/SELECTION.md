# Replacement paper trader — 2026-10-31

Selected: OkeyYT, BingX USDⓈ-M perpetuals.
ID: 1546598203650777088_1632593640919543874.

Live Kopyon API observations: 7D ROI +5.99%, 30D +17.09%, 90D +13.49%; reported max drawdown 7.7522%. The API repeats some risk statistics between periods, so these are source-reported figures, not independently reconstructed returns. Profile shows zero copiers; follower profitability is not established.

Recent 50 position-history rows have 50 distinct position IDs, with operations on each local calendar date Oct 21–31. These include partial reductions, not necessarily full closes. Position opening dates are: {"2026-10-30": 9, "2026-10-29": 5, "2026-10-28": 4, "2026-10-27": 3, "2026-10-26": 5, "2026-10-25": 2, "2026-10-24": 4, "2026-10-23": 7, "2026-08-24": 1, "2026-10-22": 3, "2026-10-21": 7}. The Aug 24 opening in recent history is a long-held exception. Median time from opening to the listed reduction/closure is about 140 minutes. The latest order-history page includes actual OPEN and CLOSE executions, with IDs, quantities, prices and position IDs. Current positions are visible and contain both gains and losses; the signal source passes the existing schema. Past daily activity does not guarantee daily new independent positions going forward.

Rejected: Harmonic Trading and Quantumxx hide current positions. WakeChang had gaps in recent daily activity and 38.21% reported 30D drawdown. ATAHO trades only BTC and uses 100x leverage. OkeyYT also scales into positions and sometimes uses 90x: our paper policy will not reproduce leverage or pyramiding, so this is a controlled prospective copying variant, not exact replication of account returns.

Switch procedure: retire FinMonster's virtual account at observed executable quotes, record funding/fees/slippage and preserve its full database. Pin the new source identity to a separate database /data/okeyyt.sqlite, initially 1000 virtual USDT. Carry forward Kopyon monthly request usage. Keep original risk 0.5% per trade, existing portfolio caps and paper-only execution; do not place live orders. Baseline history is not backfilled. Preserve 16% conservative positive-profit-share modeling assumption; profile reports 8%, but exact account settlement is not independently verified.

Validation: 110 unit tests pass, including selected trader identity after restart, baseline exclusion, wrong-account rejection, selected-ID requests, executable-price retirement, loss preservation, retirement idempotence, and request-budget carryover.

Source: https://kopyon.com/docs/bingx-copy-trading/ and raw response JSON alongside this report. Historical profitability is not proof that this paper variant will be profitable.
