"""Read-only compatibility audit. No exchange keys or trading methods."""
import argparse
from dataclasses import asdict
from decimal import Decimal
import json
import time
from urllib.request import urlopen

from .full_copy_contract import CopyBlocked, compile_contract
from .hyperliquid_feed import PublicInfo, ADDRESS
from .strict_bingx_copy import source_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--address', required=True)
    parser.add_argument('--equity', required=True, type=Decimal)
    parser.add_argument('--coins', nargs='+', default=['BTC'])
    args = parser.parse_args()
    if not ADDRESS.fullmatch(args.address) or not args.equity.is_finite() or args.equity <= 0:
        parser.error('Valid public address and positive finite equity required')
    with urlopen('https://open-api.bingx.com/openApi/swap/v2/quote/contracts', timeout=20) as response:
        result = json.load(response)
    if result.get('code') != 0:
        raise RuntimeError('Public BingX contracts unavailable')
    contracts = {c['symbol']: c for c in result['data']}
    # All data access in this command is public GET or whitelisted Info reads.
    reports = []
    for coin in args.coins:
        symbol = coin+'-USDT'
        try:
            c = contracts[symbol]
            with urlopen('https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex?symbol='+symbol, timeout=20) as response:
                mark = json.load(response)
            if mark.get('code') != 0:
                raise CopyBlocked('target_mark_unavailable')
            mark = mark['data']
            if isinstance(mark, list):
                mark = next(m for m in mark if m['symbol'] == symbol)
            rules = dict(price=mark['markPrice'],price_tick=Decimal(1).scaleb(-int(c['pricePrecision'])),
                         quantity_step=Decimal(1).scaleb(-int(c['quantityPrecision'])),
                         minimum_quantity=c['tradeMinQuantity'],minimum_notional=c['tradeMinUSDT'])
            account, orders, stamp = source_snapshot(PublicInfo(), args.address)
            contract = compile_contract(account, orders, coin, args.equity, rules, observed_ms=stamp)
            contract.entry_brackets()
            reports.append({'coin':coin,'compatible_now':True,'contract':asdict(contract)})
        except (CopyBlocked, KeyError, StopIteration) as e:
            reports.append({'coin':coin,'compatible_now':False,'reason':str(e)})
    print(json.dumps({'source':args.address,'observed_ms':time.time_ns()//1_000_000,
                      'mode':'read_only','reports':reports},ensure_ascii=False))


if __name__ == '__main__':
    main()
