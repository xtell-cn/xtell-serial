"""Burst-data stress test for the GUI (no serial hardware needed).

It feeds synthetic log lines into the receive queue at a fixed byte rate while
a 10 ms heartbeat measures how long the Tk main loop is blocked. A GUI that
keeps up shows small stalls and an empty backlog; one that does not shows long
stalls and a growing backlog.

Usage:
    python tests/stress_ui.py                      # 200 KB/s for 5 s
    python tests/stress_ui.py --rate 1000000       # 1 MB/s
    python tests/stress_ui.py --filter ERR         # with a live filter set
    python tests/stress_ui.py --module-dir D --module NAME   # test another copy
"""

import argparse
import importlib
import os
import queue
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=int, default=200_000, help="bytes per second fed to the GUI")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--filter", default="", help="live filter keyword (optional)")
    ap.add_argument("--module-dir", default=HERE)
    ap.add_argument("--module", default="xtell_serial")
    ap.add_argument("--drain-timeout", type=float, default=15.0)
    args = ap.parse_args()

    home = tempfile.mkdtemp()
    os.environ["USERPROFILE"] = home
    os.environ["HOME"] = home
    os.environ["XTELL_SERIAL_HUB_PORT"] = "50971"
    os.environ["XTERMINAL_HUB_PORT"] = "50971"
    sys.path.insert(0, args.module_dir)
    os.chdir(args.module_dir)

    import tkinter as tk
    mod = importlib.import_module(args.module)

    root = tk.Tk()
    root.geometry("900x700")
    app = mod.SerialReceiver(root)
    root.update()
    if args.filter:
        app.filter_entry.insert(0, args.filter)
        app.on_filter_changed()

    app.running = True
    app._process_rx_queue()

    stop = threading.Event()
    sent = {"bytes": 0, "lines": 0}

    def feeder():
        n = 0
        t0 = time.perf_counter()
        while not stop.is_set():
            elapsed = time.perf_counter() - t0
            if elapsed >= args.seconds:
                break
            target = int(elapsed * args.rate)
            if sent["bytes"] >= target:
                time.sleep(0.002)
                continue
            lines = []
            size = 0
            while size < 2048:
                n += 1
                tag = "ERR" if n % 50 == 0 else "INF"
                lines.append(f"[{n:08d}] {tag} sensor=0x{n & 0xFFFF:04X} value={n % 1000}.{n % 97:02d}\r\n")
                size += len(lines[-1])
            blob = "".join(lines).encode("utf-8")
            app.rx_queue.put(blob)
            sent["bytes"] += len(blob)
            sent["lines"] += len(lines)

    stalls = []
    last = [time.perf_counter()]
    worst = [0.0]

    def heartbeat():
        now = time.perf_counter()
        stalls.append(now - last[0])
        last[0] = now
        root.after(10, heartbeat)

    def watchdog():
        # measured from outside the Tk thread, so a long freeze is seen while it happens
        while not stop.is_set():
            age = time.perf_counter() - last[0]
            if age > worst[0]:
                worst[0] = age
            time.sleep(0.005)

    threading.Thread(target=watchdog, daemon=True).start()

    root.after(10, heartbeat)
    th = threading.Thread(target=feeder, daemon=True)
    th.start()

    t_start = time.perf_counter()
    drained_at = [None]

    def monitor():
        elapsed = time.perf_counter() - t_start
        if not th.is_alive():
            if app.rx_queue.qsize() == 0 and drained_at[0] is None:
                drained_at[0] = elapsed
            if drained_at[0] is not None or elapsed > args.seconds + args.drain_timeout:
                root.quit()
                return
        root.after(50, monitor)

    root.after(50, monitor)
    root.mainloop()
    stop.set()

    stalls_ms = sorted(s * 1000 for s in stalls)
    p99 = stalls_ms[int(len(stalls_ms) * 0.99) - 1] if stalls_ms else 0
    backlog = app.rx_queue.qsize()
    stored = len(app.all_lines)
    print(f"module            : {args.module}")
    print(f"feed              : {args.rate / 1000:.0f} KB/s for {args.seconds:.0f}s, {sent['lines']} lines, {sent['bytes'] / 1e6:.1f} MB")
    print(f"longest UI freeze : {worst[0] * 1000:.0f} ms")
    print(f"p99 UI stall      : {p99:.0f} ms")
    print(f"queue backlog left: {backlog} chunks")
    print(f"drained after     : {'%.1f s' % drained_at[0] if drained_at[0] is not None else 'NOT drained within timeout'}")
    print(f"lines in buffer   : {stored} (limit {app.max_lines})")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
