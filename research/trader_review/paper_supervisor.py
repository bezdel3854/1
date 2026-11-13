"""Supervise independent paper workers on one persistent Railway volume."""
import os
import signal
import subprocess
import sys
import time

ADDRESS='0x7611e4026305e5f3afa659dae315d602bbb8387d'
RTB2_ADDRESS='0xfb941d2344b72ea9101400392b818173772d0ddf'

def child_env(hyperliquid=False, *, address=ADDRESS, port='8081',
              db_path='/data/hyperliquid-paper.sqlite'):
    env=dict(os.environ)
    if hyperliquid:
        for name in ('KOPYON_API_KEY','PREVIOUS_DB_PATH','TRADER_ID','TRADER_NAME','DB_PATH'):
            env.pop(name,None)
        env.update(MODE='paper',PORT=port,HL_TRADER_ADDRESS=address,HL_DB_PATH=db_path)
    return env

def main():
    stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    specs={
        'okeyyt': ('research.trader_review.finmonster_worker',child_env()),
        'hyperliquid': ('research.trader_review.hyperliquid_worker',child_env(True)),
        'rtb2': ('research.trader_review.hyperliquid_worker',
                 child_env(True,address=RTB2_ADDRESS,port='8082',
                           db_path='/data/hyperliquid-rtb2-paper.sqlite')),
    }
    children={};retry={};attempts={}
    try:
        while not stopping:
            for name,(module,env) in specs.items():
                process=children.get(name)
                if process and process.poll() is not None:
                    attempts[name]=attempts.get(name,0)+1
                    retry[name]=time.monotonic()+min(300,5*2**min(attempts[name],6))
                    children.pop(name)
                    print('paper_worker_exited',name,process.returncode,flush=True)
                if name not in children and time.monotonic()>=retry.get(name,0):
                    children[name]=subprocess.Popen([sys.executable,'-m',module],env=env)
            time.sleep(1)
    finally:
        for process in children.values():
            if process.poll() is None:process.terminate()
        for process in children.values():
            try:process.wait(timeout=25)
            except subprocess.TimeoutExpired:process.kill();process.wait()
    return 0

if __name__=='__main__':raise SystemExit(main())
