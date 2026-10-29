"""Reproducible multi-coin RESEARCH selection from a timestamped public snapshot.

Does not import or change the running paper worker. No orders or network access.
Today's liquid symbols are not a survivorship-free historical universe.
"""
import json
import math
from pathlib import Path

DAY = 86_400_000


def select(snapshot, *, now, limit=30, minimum_volume=10_000_000,
           maximum_spread=.001, minimum_age_days=90):
    if not 1 <= limit <= 100:
        raise ValueError('Invalid research universe size')
    if not 0 <= now - snapshot['asof_ms'] <= 300_000:
        raise ValueError('Stale or future universe snapshot')
    allowed = set(snapshot['candidate_symbols'])
    contracts = {c['symbol']: c for c in snapshot['contracts']}
    seen = set()
    accepted, rejected = [], []
    for ticker in snapshot['tickers']:
        symbol = ticker['symbol']
        if symbol in seen:
            raise ValueError('Duplicate ticker')
        seen.add(symbol)
        c = contracts.get(symbol, {})
        reason = None
        try:
            volume, bid, ask = (float(ticker[k]) for k in ('quoteVolume', 'bidPrice', 'askPrice'))
            launched = int(c.get('launchTime', 0))
            if symbol not in allowed or not symbol.endswith('-USDT') or c.get('currency') != 'USDT':
                reason = 'outside explicit crypto candidate list'
            elif c.get('status') != 1 or c.get('apiStateOpen') != 'true' or c.get('apiStateClose') != 'true':
                reason = 'contract not available for opening/closing'
            elif c.get('offTime') != 0 or c.get('maintainTime') != 0:
                reason = 'maintenance or delisting scheduled'
            elif not 0 <= now - int(ticker['closeTime']) <= 300_000:
                reason = 'stale or future ticker'
            elif not all(math.isfinite(v) for v in (volume, bid, ask)) or not 0 < bid <= ask:
                reason = 'invalid volume or book'
            elif launched <= 0 or now-launched < minimum_age_days*DAY:
                reason = 'insufficient listing age'
            elif volume < minimum_volume:
                reason = 'insufficient 24h quote volume'
            else:
                spread = (ask-bid)/((ask+bid)/2)
                if spread > maximum_spread:
                    reason = 'spread too wide'
        except (KeyError, ValueError, TypeError):
            reason = 'missing or invalid market fields'
        if reason:
            rejected.append({'symbol': symbol, 'reason': reason})
        else:
            accepted.append({'symbol': symbol, 'quote_volume_usdt': volume,
                             'spread_fraction': spread, 'listing_age_days': (now-launched)/DAY})
    accepted.sort(key=lambda r: (-r['quote_volume_usdt'], r['symbol']))
    return {'asof_ms': snapshot['asof_ms'], 'research_only': True,
            'historical_selection_bias': 'Current snapshot; not a historical point-in-time universe',
            'policy': {'limit': limit, 'minimum_quote_volume_usdt': minimum_volume,
                       'maximum_spread_fraction': maximum_spread, 'minimum_listing_age_days': minimum_age_days},
            'selected': accepted[:limit], 'eligible_count': len(accepted), 'rejected': rejected}


if __name__ == '__main__':
    folder = Path(__file__).resolve().parents[2]/'reports'/'trader-review'
    snapshot = json.loads((folder/'multicoin-snapshot.json').read_text())
    # Historical reconstruction uses the snapshot's own timestamp, not wall time.
    result = select(snapshot, now=snapshot['asof_ms'])
    (folder/'multicoin-universe.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({'count': len(result['selected']), 'symbols': [r['symbol'] for r in result['selected']]}))
