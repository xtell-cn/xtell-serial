"""xtell-serial MCP 回归验证脚本 —— 以 MCP 客户端的方式驱动 xtell_serial_mcp.py。

用法:
    python tests/probe_mcp.py                  # 跑基础用例 (T1/T2/T6)
    python tests/probe_mcp.py --fake           # 拉起假 GUI 跑全链路测试 (T1/T2/T3/T6/T7)
    python tests/probe_mcp.py --exe            # 验证打包后的 dist/xtell-serial-mcp.exe
    python tests/probe_mcp.py --port COM3      # 接真实硬件端到端验证
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER = os.path.join(BASE, "xtell_serial_mcp.py")
HUB_PORT = int(os.environ.get("XTELL_SERIAL_HUB_PORT", "50016"))

PASS, FAIL = "PASS", "FAIL"
results = []


def record(name, ok, detail=""):
    results.append((name, PASS if ok else FAIL, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" - {detail}" if detail else ""))


ADAPTER_CMD = [sys.executable, ADAPTER]      # 可被 --exe 覆盖为打包后的 exe


class Client:
    """最小 MCP stdio 客户端。"""

    def __init__(self):
        self.p = subprocess.Popen(
            ADAPTER_CMD, cwd=BASE,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.buf = {}
        self.lock = threading.Lock()
        self.next_id = 1
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _reader(self):
        for line in self.p.stdout:
            line = line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # stdout 被污染 —— 这本身就是一个失败信号
                with self.lock:
                    self.buf.setdefault("__garbage__", []).append(line)
                continue
            if "id" in msg:
                with self.lock:
                    self.buf[msg["id"]] = msg

    def _drain_stderr(self):
        for _ in self.p.stderr:
            pass

    def call(self, method, params=None, timeout=10.0):
        rid = self.next_id
        self.next_id += 1
        self.p.stdin.write((json.dumps({
            "jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}
        }) + "\n").encode())
        self.p.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if rid in self.buf:
                    return self.buf.pop(rid)
            time.sleep(0.02)
        return None

    def notify(self, method, params=None):
        self.p.stdin.write((json.dumps({
            "jsonrpc": "2.0", "method": method, "params": params or {}
        }) + "\n").encode())
        self.p.stdin.flush()

    def handshake(self):
        t0 = time.time()
        res = self.call("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "probe", "version": "1"}})
        elapsed = time.time() - t0
        self.notify("notifications/initialized")
        return res, elapsed

    def tool(self, name, args, timeout=15.0):
        res = self.call("tools/call", {"name": name, "arguments": args}, timeout)
        if not res or "result" not in res:
            return None
        return "\n".join(c.get("text", "") for c in res["result"].get("content", []))

    def garbage(self):
        with self.lock:
            return self.buf.get("__garbage__", [])

    def close(self):
        try:
            self.p.kill()
        except Exception:
            pass


def hub_alive():
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", HUB_PORT))
        return True
    except Exception:
        return False
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="真实串口, 提供后才跑 T3/T4/T7")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--fake", action="store_true",
                    help="拉起无界面假 GUI(tests/fake_gui.py)跑全链路, 不需要真实硬件")
    ap.add_argument("--capture", type=float, default=5.0, help="真实串口抓数据的秒数")
    ap.add_argument("--filter", default="]", help="真实串口用例里设置的过滤关键字")
    ap.add_argument("--send", default="\r\n", help="真实串口用例里发送的内容")
    ap.add_argument("--exe", action="store_true",
                    help="改用打包后的 dist/xtell-serial-mcp.exe, 验证实际注册的那个文件")
    args = ap.parse_args()

    if args.exe:
        global ADAPTER_CMD
        exe = os.path.join(BASE, "dist", "xtell-serial-mcp.exe")
        if not os.path.exists(exe):
            print(f"找不到 {exe}, 请先执行 build_exe.bat")
            sys.exit(2)
        ADAPTER_CMD = [exe]
        print(f"被测对象: {exe}\n")

    fake = None
    if args.fake:
        args.port = args.port or "COM99"
        fake = subprocess.Popen(
            [sys.executable, os.path.join(BASE, "tests", "fake_gui.py"),
             "--port", args.port, "--seconds", "60"],
            cwd=BASE, stderr=subprocess.PIPE)
        threading.Thread(
            target=lambda: [sys.stderr.write("    " + l.decode("utf-8", "replace"))
                            for l in fake.stderr], daemon=True).start()
        for _ in range(30):
            if hub_alive():
                break
            time.sleep(0.2)

    running = hub_alive()
    print(f"Hub 状态: {'运行中' if running else '未运行'} (127.0.0.1:{HUB_PORT})\n")

    c = Client()
    try:
        print("T1/T2 握手与工具清单")
        res, elapsed = c.handshake()
        record("initialize 有响应", bool(res and "result" in res))
        record("initialize 在 5s 内返回(含冷启动)", elapsed < 5.0, f"{elapsed:.3f}s")

        t0 = time.time()
        lst = c.call("tools/list")
        tools = lst["result"]["tools"] if lst and "result" in lst else []
        record("tools/list 返回 14 个工具", len(tools) == 14, f"实际 {len(tools)}")
        record("tools/list 在 1s 内返回", time.time() - t0 < 1.0)
        record("stdout 未被日志污染", not c.garbage(), str(c.garbage()[:1]))

        text = c.tool("list_serial_ports", {})
        record("list_serial_ports 有结果", bool(text), (text or "")[:60])
        if not running:
            record("T1 无 Hub 时给出可读提示",
                   text is not None and "open the xtell-serial program" in text, (text or "")[:60])

        print("\nT6 双适配器并发")
        c2 = Client()
        try:
            c2.handshake()
            a = c.tool("list_serial_ports", {})
            b = c2.tool("list_serial_ports", {})
            record("两个适配器实例互不串扰", bool(a) and bool(b) and a == b)
        finally:
            c2.close()

        if args.fake and running:
            print(f"\nT3/T7 全链路 (假 GUI, 串口 {args.port})")
            t = c.tool("send_serial_data", {"port": args.port, "data": "AT\r\n"})
            record("send_serial_data 命中已注册窗口", bool(t) and "Sent" in t, (t or "")[:80])
            t = c.tool("send_serial_data", {"port": args.port.lower(), "data": "AT\r\n"})
            record("端口名大小写容错", bool(t) and "Sent" in t, (t or "")[:80])
            t = c.tool("send_serial_data", {"port": args.port, "data": "   "})
            record("空内容如实报错(不谎报成功)",
                   bool(t) and "Nothing to send" in t, (t or "")[:80])
            t = c.tool("read_received_data", {"port": args.port, "lines_count": 5})
            record("read_received_data 跨连接回传", bool(t) and "fake main" in t, (t or "")[:60])
            t = c.tool("get_buffer_status", {"port": args.port})
            record("get_buffer_status 回报缓存状态", bool(t) and "Buffer status" in t, (t or "")[:60])
            t = c.tool("read_sent_data", {"port": args.port, "lines_count": 5})
            record("read_sent_data 跨连接回传", bool(t) and "fake sent" in t, (t or "")[:60])
            t = c.tool("set_filter", {"port": args.port, "keyword": "ERR"})
            record("set_filter", bool(t) and "Filter set" in t, (t or "")[:80])
            t = c.tool("send_serial_data", {"port": "COM_NOT_EXIST", "data": "x"})
            record("未知串口给出可读错误",
                   bool(t) and "No window is managing" in t, (t or "")[:80])
            t = c.tool("connect_serial", {"port": "COM_NOT_EXIST", "baudrate": 115200})
            record("connect 到不存在的串口给出可读错误",
                   bool(t) and "does not exist on this computer" in t, (t or "")[:80])
        elif args.port and running:
            print(f"\nT3/T4/T7 真实串口链路 ({args.port} @ {args.baud})")
            t = c.tool("connect_serial", {"port": args.port, "baudrate": args.baud})
            record("connect_serial 回报真实结果", bool(t) and "Connected" in t, (t or "")[:80])
            print(f"    抓取 {args.capture}s 数据 ...")
            time.sleep(args.capture)
            t = c.tool("read_received_data", {"port": args.port, "lines_count": 20})
            lines = [l for l in (t or "").splitlines()
                     if l.strip() and "No matching data" not in l]
            record(f"read_received_data 抓到数据", len(lines) > 0, f"{len(lines)} 行" if lines else "该串口静默, 无数据")
            t = c.tool("set_filter", {"port": args.port, "keyword": args.filter})
            record("set_filter", bool(t) and "Filter set" in t, (t or "")[:60])
            time.sleep(1.0)
            t = c.tool("read_filtered_data", {"port": args.port, "lines_count": 10})
            record("read_filtered_data 有返回", t is not None, (t or "").splitlines()[:1])
            t = c.tool("send_serial_data", {"port": args.port, "data": args.send})
            record("send_serial_data 回报真实结果", bool(t) and "Sent" in t, (t or "")[:60])
            t = c.tool("send_serial_data", {"port": args.port, "data": "   "})
            record("空内容发送如实报错(不谎报成功)", bool(t) and "Nothing to send" in t, (t or "")[:60])
            time.sleep(1.0)
            t = c.tool("read_sent_data", {"port": args.port, "lines_count": 5})
            ok = bool(t) and args.send.strip() in t
            record("read_sent_data 能查到刚发的内容", ok, (t or "")[:60])
            t = c.tool("disconnect_serial", {"port": args.port})
            record("disconnect_serial", bool(t) and "Disconnected" in t, (t or "")[:60])
        elif args.port:
            print("\n跳过 T3/T4/T7: Hub 未运行, 请先打开 xtell-serial")
        else:
            print("\n跳过 T3/T4/T7: 未提供 --port")
    finally:
        c.close()
        if fake:
            fake.kill()

    ok = sum(1 for _, r, _ in results if r == PASS)
    print(f"\n{'=' * 50}\n通过 {ok}/{len(results)}")
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    main()
