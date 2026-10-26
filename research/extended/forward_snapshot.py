"""Compact, lossless post-freeze capture relative to the already versioned BingX history."""
from pathlib import Path
from dataclasses import asdict
import gzip,hashlib,json
from bingx_bot.data import load_dataset
from bingx_bot.config import SYMBOLS,BAR


def pack(base='data/history',snapshot='data/post-freeze'):
    base_cs,base_fs,_,base_manifest=load_dataset(base)
    cs,fs,rs,manifest=load_dataset(snapshot)
    payload={'base_sha256':base_manifest['sha256'],'source_sha256':manifest['sha256'],'symbols':{}}
    for s in SYMBOLS:
        old_cs={c.ts:c for c in base_cs[s]};old_fs={f.ts:f for f in base_fs[s]}
        payload['symbols'][s]={'start':cs[s][0].ts,'end':cs[s][-1].ts+BAR,'rules':asdict(rs[s]),
            'candles':[asdict(c) for c in cs[s] if old_cs.get(c.ts)!=c],
            'funding':[asdict(f) for f in fs[s] if old_fs.get(f.ts)!=f]}
    path=Path(snapshot)/'delta.json';path.write_text(json.dumps(payload,sort_keys=True))
    return {s:{'new_or_corrected_candles':len(x['candles']),'new_or_corrected_funding':len(x['funding'])} for s,x in payload['symbols'].items()}


def reconstruct(base='data/history',snapshot='data/post-freeze',out=None):
    cs,fs,_,base_manifest=load_dataset(base)
    payload=json.loads((Path(snapshot)/'delta.json').read_text())
    if payload['base_sha256']!=base_manifest['sha256']:raise ValueError('Wrong base capture')
    dest=Path(out or snapshot);dest.mkdir(parents=True,exist_ok=True)
    for s in SYMBOLS:
        meta=payload['symbols'][s]
        c={v.ts:asdict(v) for v in cs[s]};f={v.ts:asdict(v) for v in fs[s]}
        c.update({v['ts']:v for v in meta['candles']});f.update({v['ts']:v for v in meta['funding']})
        data={'candles':[v for t,v in sorted(c.items()) if meta['start']<=t<meta['end']],
              'funding':[v for t,v in sorted(f.items()) if meta['start']<=t<meta['end']], 'rules':meta['rules']}
        raw=json.dumps(data,sort_keys=True).encode()
        if hashlib.sha256(raw).hexdigest()!=payload['source_sha256'][s]:raise ValueError('Reconstruction checksum mismatch')
        (dest/f'{s}.json.gz').write_bytes(gzip.compress(raw,mtime=0))
    (dest/'manifest.json').write_bytes((Path(snapshot)/'manifest.json').read_bytes())
    load_dataset(dest)
    return payload['source_sha256']

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--pack',action='store_true');p.add_argument('--out')
    args=p.parse_args()
    print(json.dumps(pack() if args.pack else reconstruct(out=args.out)))
