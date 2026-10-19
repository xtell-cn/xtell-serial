"""无界面的假 GUI client, 用于在没有真实硬件/桌面的情况下回归 Hub 链路。

它复用真实的 hub_server 模块: 先尝试成为 Hub, 再以 role=gui 的 client 身份
连上去, 声称自己管理着 --port 指定的串口, 并按真实 GUI 的协议应答指令。

用法: python tests/fake_gui.py --port COM99 [--seconds 30]
"""

import argparse
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub_server   # noqa: E402

received = []       # 收到的 Hub 指令, 供断言查看


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM99")
    ap.add_argument("--name", default="fake-gui")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--slow-ack", type=float, default=0.0,
                    help="回 ack 前先睡这么多秒, 用于验证 Hub 的 ack 超时文案")
    args = ap.parse_args()

    became_hub = hub_server.try_become_hub()
    print(f"[fake_gui] try_become_hub -> {became_hub} (port {hub_server.HUB_PORT})",
          file=sys.stderr, flush=True)
    time.sleep(0.3)

    s = socket.socket()
    s.connect((hub_server.HUB_HOST, hub_server.HUB_PORT))

    def send(obj):
        s.sendall((json.dumps(obj) + "\n").encode("utf-8"))

    send({"cmd": "hello", "role": "gui", "name": args.name, "pid": os.getpid()})
    send({"cmd": "status", "port": args.port, "state": "connected", "name": args.name})
    print(f"[fake_gui] registered as {args.port}", file=sys.stderr, flush=True)

    def reader():
        for line in s.makefile("r", encoding="utf-8"):
            if not line.strip():
                continue
            msg = json.loads(line.strip())
            received.append(msg)
            print(f"[fake_gui] <- {msg}", file=sys.stderr, flush=True)
            if msg.get("cmd") == "query":
                send({"cmd": "response", "req_id": msg.get("req_id"),
                      "data": [f"[00:00:00.000] fake {msg.get('target')} line 1",
                               f"[00:00:00.001] fake {msg.get('target')} line 2"]})
            elif msg.get("cmd") == "send":
                if args.slow_ack:
                    time.sleep(args.slow_ack)
                data = msg.get("data", "")
                if data.strip():
                    send({"cmd": "ack", "req_id": msg.get("req_id"), "ok": True,
                          "text": f"已发送: {data.strip()}"})
                else:
                    send({"cmd": "ack", "req_id": msg.get("req_id"), "ok": False,
                          "text": "发送内容为空, 已忽略"})
            elif msg.get("cmd") == "connect":
                if args.slow_ack:
                    time.sleep(args.slow_ack)
                send({"cmd": "status", "port": msg.get("port"), "state": "connected",
                      "name": args.name})
                send({"cmd": "ack", "req_id": msg.get("req_id"), "ok": True,
                      "text": f"已连接 {msg.get('port')} {msg.get('baudrate')}bps"})
            elif msg.get("cmd") == "disconnect":
                send({"cmd": "status", "port": "", "state": "idle", "name": args.name})
            elif msg.get("cmd") == "get_buffer_status":
                if args.slow_ack:
                    time.sleep(args.slow_ack)
                send({"cmd": "ack", "req_id": msg.get("req_id"), "ok": True,
                      "text": "缓存状态: 已使用 2 行, 上限 100000 行, 剩余 99998 行 (占用: 0.00%)"})

    threading.Thread(target=reader, daemon=True).start()

    deadline = time.time() + args.seconds
    try:
        while time.time() < deadline:
            time.sleep(0.1)
    finally:
        s.close()


if __name__ == "__main__":
    main()
