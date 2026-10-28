# Public API recovery fix — 2026-10-28

## Cause

BingX gateway code100410 means request frequency protection. The exact effective
limit for a hosting source IP is not established by this incident. The previous
client treated business-code throttling as an ordinary ValueError, bypassing
its existing HTTP/network retries. The worker then rebuilt a new PublicClient
and both strategy histories after every exception. Each recovery attempt could
fetch4096 15m bars per asset, rather than retaining completed indicators; this
amplified traffic and discarded client pacing state. Five consecutive failures
terminated the process. A bounded hosting restart budget cannot guarantee
recovery from a sustained gateway outage, so a manual restart was required.

## Changed behavior

Only known transient public API failures (100410,100500,100503, HTTP418/429,
exhausted5xx/network retries) enter the recovery path. A process-wide client
cooldown is enforced before more requests; HTTP Retry-After is honored when
numeric. On100410, the worker waits at least60s, then120/240/up to300s with
jitter. HTTP418 waits at least1200s. These are conservative client choices,
not claims about a documented exchange quota. Public requests are paced at
least2s apart in this process. No alternate IP, account, or endpoint evasion.

Known outages no longer consume a five-error fatal budget. The existing client,
limiter, and completed features survive. BTC/ETH indicator updates are staged
and published together, preventing double-processing BTC if ETH fails.

During outage health returns503, no account execution takes place, funding
cursor does not advance and stale quotes cannot be used for fills. Recovery
resumes from committed state, records api_recovered, and old entry windows
are still skipped. Errors and retry delays are persisted in the local journal
as api_unavailable. Unknown programming/data-integrity errors retain the prior
bounded fatal path. MODE remains paper; strategy parameters/risk unchanged.

## Verification

74 unit tests pass locally. Added simulated business and HTTP rate limits,
Retry-After, cross-endpoint cooldown, exhausted transport failures, permanent
parameter-error rejection, partial-symbol failure/retry, seven outages followed
by recovery with one client and one warmup, and fail-fast once mode.

The actual worker lifecycle smoke checks HTTP503 on each of seven outages,
HTTP200 after recovery, unchanged virtual balance/no entries, persisted outage
and recovery events, graceful SIGTERM and durable restart. It makes no external
requests and is also run in the Docker CI job. Production deployment and CI
results are verified separately after publication; this document alone is not
proof of uninterrupted future operation or profitability.

Primary references:
https://github.com/BingX-API/api-ai-skills/blob/main/skills/references/error-codes.md
https://github.com/BingX-API/api-ai-skills/blob/main/skills/references/rate-limits.md
