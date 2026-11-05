"""Supervise two independent paper workers on one persistent Railway volume."""
import os
import signal
import subprocess
import sys
import time

ADDRESS='0x7611e4026305e5f3afa659dae315d602bbb8387d'

def child_env(hyperliquid=False):
    env=dict(os.environ)
    if hyperliquid:
        for name in ('KOPYON_API_KEY','PREVIOUS_DB_PATH','TRADER_ID','TRADER_NAME','DB_PATH'):
            env.pop(name,None)
        env.update(MODE='paper',PORT='8081',HL_TRADER_ADDRESS=ADDRESS,
                   HL_DB_PATH='/data/hyperliquid-paper.sqlite')
    return env

def main():
    stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    specs=[('research.trader_review.finmonster_worker',False),
           ('research.trader_review.hyperliquid_worker',True)]
    children={};retry={};attempts={}
    try:
        while not stopping:
            for module,hl in specs:
                process=children.get(module)
                if process and process.poll() is not None:
                    attempts[module]=attempts.get(module,0)+1
                    retry[module]=time.monotonic()+min(300,5*2**min(attempts[module],6))
                    children.pop(module)
                    print('paper_worker_exited',module,process.returncode,flush=True)
                if module not in children and time.monotonic()>=retry.get(module,0):
                    children[module]=subprocess.Popen([sys.executable,'-m',module],env=child_env(hl))
            time.sleep(1)
    finally:
        for process in children.values():
            if process.poll() is None:process.terminate()
        for process in children.values():
            try:process.wait(timeout=25)
            except subprocess.TimeoutExpired:process.kill();process.wait()
    return 0

if __name__=='__main__':raise SystemExit(main())
