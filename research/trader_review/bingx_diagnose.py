"""Inspect a stopped copy journal and validate requests without placing orders."""
import argparse
import json
import os
import sqlite3

from .bingx_execution import BingXExecution


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--validate', action='store_true',
                        help='Use BingX order/test; no real orders are created')
    args = parser.parse_args()
    db = sqlite3.connect('file:' + args.db + '?mode=ro', uri=True)
    db.execute('PRAGMA query_only=ON')
    execution = BingXExecution(os.environ['BINGX_API_KEY'], os.environ['BINGX_API_SECRET'],
                               ':memory:', trading_enabled=False)
    for table in ('copy_cutover', 'copy_plans', 'copied_positions', 'copied_signals'):
        print(json.dumps({table: db.execute('SELECT * FROM ' + table).fetchall()}), flush=True)
    print(json.dumps({'polls': db.execute(
        'SELECT observed_ms,payload FROM polls ORDER BY observed_ms DESC LIMIT 3').fetchall()}), flush=True)
    for path in ('trade/positionSide/dual', 'user/positions'):
        try:
            print(json.dumps({path: execution._request('GET', path)}), flush=True)
        except Exception as error:
            print(json.dumps({path: str(error)}), flush=True)
    allowed = {'symbol', 'side', 'positionSide', 'type', 'quantity', 'clientOrderId', 'reduceOnly'}
    for client_id, raw in db.execute('SELECT client_id,parameters FROM bingx_submissions'):
        parameters = json.loads(raw)
        if set(parameters) - allowed:
            raise ValueError('Unexpected saved request fields; refusing to transmit them')
        print(json.dumps({'saved_request': parameters}), flush=True)
        for method, path in [('GET', 'trade/order')] + (
                [('POST', 'trade/order/test')] if args.validate else []):
            query = parameters if method == 'POST' else {
                'symbol': parameters['symbol'], 'clientOrderId': client_id}
            try:
                print(json.dumps({path: execution._request(method, path, **query)}), flush=True)
            except Exception as error:
                print(json.dumps({path: str(error)}), flush=True)


if __name__ == '__main__':
    main()
