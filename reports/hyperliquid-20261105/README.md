# Hyperliquid independent paper experiment

Public source: `0x7611e4026305e5f3afa659dae315d602bbb8387d`. Selected from public leaderboard and fill/portfolio/account snapshots. Activity on each of seven completed UTC days, HYPE/LIT/NEAR. Source uses 10x; copied account keeps existing 0.5% per-execution risk, 1% aggregate budget and 2x ceiling. Not a profitability claim.

Two supervised workers share the existing Railway container but use separate processes, ports, SQLite files and balances. OkeyYT remains at port 8080 with `/data/okeyyt.sqlite`. Hyperliquid uses port 8081 and `/data/hyperliquid-paper.sqlite`, initially 1000 USDT. Hyperliquid child does not inherit Kopyon key or previous-account migration variables. No exchange keys or order endpoint exists in the source client.

The first poll creates a historical baseline without simulated entries. Next fills are durably deduplicated; opens, increases, partial closes and reversals are normalized. Overlapping anchors and position continuity checks fail closed on gaps. Fresh entries require age <=120 seconds and executable BingX spread/drift checks. Quotes, fees, funding and stops use the existing paper engine. Native crypto perpetuals only; unsupported contracts and HIP-3/spot are excluded. No platform profit share is invented for this independent experiment.

Run: `MODE=paper HL_TRADER_ADDRESS=0x7611e4026305e5f3afa659dae315d602bbb8387d HL_DB_PATH=/data/hyperliquid-paper.sqlite PORT=8081 python -m research.trader_review.hyperliquid_worker`

The Railway Dockerfile.finmonster now launches paper_supervisor, which restarts an exited child with backoff and forwards shutdown to both children. Persistent journals survive deployment. `/health` on each port requires a successful source cycle younger than 180 seconds. `/ready` is process readiness; `/status` reports the independent account.

Validation: full unit suite, including baseline/restart/dedup, reversal, position-gap rollback, proportional source reduction after joining on a new add, and credential/database separation. Historical source results do not establish copied profitability.
