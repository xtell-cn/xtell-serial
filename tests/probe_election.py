"""xtell-serial Hub 选举与重新推举回归验证。

用法: python tests/probe_election.py [--port 50057]
依赖 psutil。
"""

import argparse
import os
import subprocess
import sys
import time

try:
    import psutil
except ImportError:
    print("需要安装 psutil: pip install psutil")
    sys.exit(0)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILOVER_BUDGET = 2.0


def listeners(port):
    return sorted(c.pid for c in psutil.net_connections(kind="tcp")
                  if c.laddr and c.laddr.port == port and c.status == "LISTEN")


def wait_listen(port, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        pids = listeners(port)
        if pids:
            return pids
        time.sleep(0.02)
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=50057)
    args = ap.parse_args()
    env = dict(os.environ, XTELL_SERIAL_HUB_PORT=str(args.port))
    fails = []

    def check(name, ok, detail=""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    g1 = g2 = None
    try:
        g1 = subprocess.Popen([sys.executable, "xtell_serial.py"], cwd=BASE, env=env,
                              stderr=subprocess.DEVNULL)
        pids = wait_listen(args.port)
        check("GUI#1 成为 Hub", pids == [g1.pid], f"listeners={pids}")

        g2 = subprocess.Popen([sys.executable, "xtell_serial.py"], cwd=BASE, env=env,
                              stderr=subprocess.DEVNULL)
        time.sleep(3)
        pids = wait_listen(args.port)
        check("GUI#2 回落为 client, 仍由 GUI#1 独占监听",
              pids == [g1.pid], f"listeners={pids}")

        g1.kill()
        g1.wait(timeout=2)
        t_kill = time.time()
        while time.time() - t_kill < FAILOVER_BUDGET:
            pids = listeners(args.port)
            if pids == [g2.pid]:
                break
            time.sleep(0.02)
        elapsed = time.time() - t_kill
        check(f"GUI#2 在 {FAILOVER_BUDGET}s 内接管成为新 Hub",
              pids == [g2.pid], f"{elapsed:.3f}s (listeners={pids})")

    finally:
        for p in (g1, g2):
            if p:
                try:
                    p.kill()
                except Exception:
                    pass

    ok = not bool(fails)
    print(f"\n{'=' * 50}\n{'ALL PASS' if ok else 'FAILED: ' + str(fails)}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
