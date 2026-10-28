# Continuation checkpoint — 26 October 2026

User goal: autonomously develop and verify a BTC/ETH USDT perpetual BingX bot. Virtual deposit 1,000 USDT, planned risk <=0.5% per entry, Long/Short, 15m + completed 1h. No real orders or exchange keys. User authorizes independent research and asks for final results, not repeated questions.

264 total configurations tried: original eight all failed; extended 256 fixed configurations trained on Binance public USD-M 2023, validated 2024, one finalist frozen before 2025. Binance data are a cross-venue research proxy, not BingX execution evidence. See reports/extended/REPORT.md, research/extended/PROTOCOL.md, reports/extended/frozen.json. Original protocol hash preserved. Do not reselect after reading 2025 and call it independent evidence.

Frozen candidate: candidate_120h, spec 56, Donchian previous 120 completed hours, EMA32/128 hour trend filter, 2 ATR initial stop, 3 ATR trail, 4R target, max hold 7 days, min ATR/price 0.15%, no martingale. Pure stdlib streaming implementation bingx_bot/frozen_candidate.py agrees with research reference on all BingX bars.

Results with costs/funding: 2023 train +10.3468%; 2024 validation +17.0165%; 2025 untouched selection test +9.6702% (195 trades, max open/close DD 5.8387%); 2025 stressed costs +1.4139%. Known BingX 2026-08-11 to 2026-10-25 development diagnostic +2.7899% (37 trades, DD 5.4213%). Continuous 2023–2025 standard +41.1234%, DD 8.8164%, no halt; continuous stressed +3.8811%, DD 10.0411%, HALTED. Most 2025 profit came from ETH. 264-trial adjusted bootstrap lower bound negative (-24.6999%). Thus NOT proven profitable and not promoted for automatic entries.

65 tests passed locally. Live public-data smoke completed with candidate_120h, observe_only=true, equity=1000, no positions. Default paper mode observes only. Real-order code absent. Railway Docker/IaC prepared but no Railway deployment exists. Prior GitHub Actions passed on old revision; check current CI after checkpoint publication.

Big proxy files excluded from Git; fetch command and SHA256 manifest retained. PYTHONPATH=. python research/extended/fetch_proxy.py; pip install '.[research]' for numpy/pandas. Full replay: research/extended/sweep.py, audit_finalist.py, report.py. Reproduction is NOT a new holdout. Daily curves/trades/results retained in Git.

Next: finish/check current CI, verify health/startup and persistence, inspect failed cost robustness without cherry-picking, then obtain genuinely new data for any changed model. Keep real trading disabled. Do not promise guaranteed profit or imply an autonomous cloud service is running.


## Resumed 2026-10-26

The complete source/metrics checkpoint was published as e66b939a3925f51abac29a566d5f9e596af94c09. GitHub Actions 40262523790 passed 65 tests, replay and container build on that revision.

Additional work: exact post-freeze BingX replay 2026-10-25 10:15 UTC to 2026-10-26 03:00 UTC, 16.75h, zero entries, 1000 USDT. NOT actual live/paper fills. New snapshot reconstructed losslessly from versioned data/history plus data/post-freeze/delta.json; source SHA256 verified. New scripts forward_check.py / forward_snapshot.py, report reports/post-freeze/REPORT.md.

66 tests pass locally. Actual worker lifecycle test with fake public API checks initial health503, ready200, SIGTERM, socket closure, persistent account restart; added to Docker CI. A missing explicit server_close() was fixed. Current CI after this continuation must be checked. Railway still not deployed, no real orders. Candidate parameters unchanged, experimental paper entries remain disabled by default.
