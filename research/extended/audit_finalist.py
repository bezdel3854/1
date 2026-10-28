"""Post-freeze diagnostics; no reselection, no parameter changes."""
import json
from pathlib import Path
from .sweep import stamp,bank,eval_spec
from bingx_bot.data import load_dataset
from bingx_bot.config import SYMBOLS


def audit():
    result=json.loads(Path('reports/extended/results.json').read_text())
    s=result['freeze']['selected']
    if s is None:return
    cs,fs,rs,_=load_dataset('data/proxy');features={k:bank(cs[k]) for k in SYMBOLS}
    additional={}
    for name,stress in [('continuous_2023_2025',False),('continuous_2023_2025_stress',True)]:
        m,_=eval_spec(cs,fs,rs,features,s,stamp('2023-01-01'),stamp('2026-01-01'),stress=stress,
                      artifacts='reports/extended/'+name)
        additional[name]=m
    quarters=[]
    for year in (2023,2024,2025):
        for month in (1,4,7,10):
            end=f'{year+1}-01-01' if month==10 else f'{year}-{month+3:02d}-01'
            m,_=eval_spec(cs,fs,rs,features,s,stamp(f'{year}-{month:02d}-01'),stamp(end))
            quarters.append({'year':year,'quarter':(month-1)//3+1,**m})
    additional['quarters_reset_capital']=quarters
    additional['warning']='Post-freeze descriptive diagnostics. 2023/2024 used for selection; continuous period includes development. No reselection.'
    Path('reports/extended/additional_audit.json').write_text(json.dumps(additional,indent=2))
    print(json.dumps({k:v for k,v in additional.items() if k!='quarters_reset_capital'},indent=2))

if __name__=='__main__':audit()
