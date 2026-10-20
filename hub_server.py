"""xtell-serial Hub —— GUI 进程内的纯 TCP 中枢（不含任何 stdio 代码）。

角色约定:
  Hub        : 第一个成功 bind 50016 的 GUI, 同时也是一个 client
  client     : 后续 GUI (bind 失败者) 以及 MCP server
  MCP server : 由 Claude / Antigravity 拉起的薄适配层, 只连不占端口

Hub 负责: 维护 client 路由表、把 MCP server 的 rpc 请求转发给管理目标串口的
GUI client、把 GUI 的查询结果按 req_id 跨连接回传给发起请求的 MCP server。
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import uuid

import serial.tools.list_ports

HUB_HOST = "127.0.0.1"
HUB_PORT = int(os.environ.get("XTELL_SERIAL_HUB_PORT", "50016"))
SRV_TAG = "xtell_serial_hub"
SRV_VERSION = "1.0"

# 等待新拉起的 GUI client 上线的最长时间(秒)
SPAWN_WAIT_TIMEOUT = 8.0
# 等待 GUI 回报动作执行结果的最长时间(秒)
ACK_TIMEOUT = float(os.environ.get("XTELL_SERIAL_ACK_TIMEOUT", "10"))
# 需要等 GUI 回报真实结果的动作(其余动作 fire-and-forget)
ACK_ACTIONS = ("connect", "send", "get_buffer_status")

# conn_id -> {"writer", "role", "port", "state", "name"}
clients = {}
# req_id -> conn_id (发起该查询的 MCP server 连接), 用于结果跨连接回传
pending = {}
# req_id -> Future, 等待 GUI 的 ack
ack_futures = {}

_hub_thread = None
_is_hub = False


def log(msg):
    """Hub 日志一律写 stderr; 窗口版 exe 下 stderr 可能为 None, 需吞掉异常。"""
    try:
        sys.stderr.write(f"[xtell_serial_hub] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def normalize_port(p):
    """串口名归一化, 容错大小写与空格。"""
    return (p or "").strip().upper()


def list_local_ports():
    return [p.device for p in serial.tools.list_ports.comports()]


###############################################################################
# 路由表查询 —— 两个查找函数都必须过滤 role == "gui",
# 否则会把 MCP server 当成空闲窗口, 串口指令将发进黑洞。
###############################################################################

def find_gui_by_port(port):
    p = normalize_port(port)
    hits = [c for c in clients.values()
            if c["role"] == "gui" and normalize_port(c["port"]) == p and p]
    return hits[-1] if hits else None      # 多窗口声称同一口时取最新上报者


def find_idle_gui():
    for c in clients.values():
        if c["role"] == "gui" and (c["state"] == "idle" or not c["port"]):
            return c
    return None


async def wait_for_idle_gui(timeout=SPAWN_WAIT_TIMEOUT):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        gui = find_idle_gui()
        if gui:
            return gui
        await asyncio.sleep(0.2)
    return None


def spawn_gui_client():
    """按绝对路径拉起一个新的 GUI client(不依赖 cwd)。"""
    base = os.path.dirname(os.path.abspath(__file__))
    try:
        if getattr(sys, "frozen", False):
            exe = os.path.join(os.path.dirname(sys.executable), "xtell-serial.exe")
            subprocess.Popen([exe], cwd=base)
        else:
            subprocess.Popen([sys.executable, os.path.join(base, "xtell_serial.py")], cwd=base)
        return True
    except Exception as e:
        log(f"spawn gui client failed: {e}")
        return False


###############################################################################
# 报文收发
###############################################################################

async def send(writer, obj):
    writer.write((json.dumps(obj) + "\n").encode("utf-8"))
    await writer.drain()


async def reply(mcp_cid, req_id, ok, text):
    c = clients.get(mcp_cid)
    if not c:
        return
    try:
        await send(c["writer"], {"cmd": "rpc_result", "req_id": req_id,
                                 "ok": ok, "text": text})
    except Exception as e:
        log(f"reply to {mcp_cid} failed: {e}")


###############################################################################
# rpc 分发
###############################################################################

QUERY_TARGET = {"query_main": "main", "query_sent": "sent", "query_filter": "filter"}


def to_gui_cmd(action, msg):
    """把 MCP 动作翻译成 GUI client 能识别的指令。"""
    if action == "connect":
        return {"cmd": "connect", "port": msg.get("port"), "baudrate": msg.get("baudrate")}
    if action == "disconnect":
        return {"cmd": "disconnect", "port": msg.get("port")}
    if action == "send":
        return {"cmd": "send", "data": msg.get("data", ""),
                "hex_mode": msg.get("hex_mode", False)}
    if action == "auto_send_on":
        return {"cmd": "auto_send", "enable": True, "data": msg.get("data", ""),
                "interval": msg.get("interval_ms", 1000),
                "hex_mode": msg.get("hex_mode", False)}
    if action == "auto_send_off":
        return {"cmd": "auto_send", "enable": False}
    if action == "set_filter":
        return {"cmd": "filter", "keyword": msg.get("keyword", ""),
                "is_regex": msg.get("is_regex", False),
                "enable": msg.get("enable", True)}
    if action == "save_on":
        return {"cmd": "save_file", "enable": True, "filepath": msg.get("filepath")}
    if action == "save_off":
        return {"cmd": "save_file", "enable": False}
    if action == "clear":
        return {"cmd": "clear", "target": msg.get("target", "all")}
    if action == "get_buffer_status":
        return {"cmd": "get_buffer_status"}
    raise ValueError(f"unknown action: {action}")


def ok_text(action, port, msg, win_name=""):
    where = f"{port}" + (f" (window: {win_name})" if win_name else "")
    return {
        "connect": f"Asked the window to connect {where}.",
        "disconnect": f"Disconnected {where}.",
        "send": f"Sent to {where}: {msg.get('data', '')}",
        "auto_send_on": f"Started repeat sending on {where}, interval {msg.get('interval_ms', 1000)} ms.",
        "auto_send_off": f"Stopped repeat sending on {where}.",
        "set_filter": f"Filter set on {where}: {msg.get('keyword', '')}",
        "save_on": f"Started saving data from {where} to {msg.get('filepath')}.",
        "save_off": f"Stopped saving data from {where}.",
        "clear": f"Cleared the display buffer of {where} ({msg.get('target', 'all')}).",
        "get_buffer_status": f"Buffer status query for {where} completed.",
    }.get(action, f"{action} executed on {where}.")


async def dispatch_rpc(mcp_cid, msg):
    action = msg.get("action")
    req_id = msg.get("req_id")

    if action == "list_ports":
        return await reply(mcp_cid, req_id, True,
                           f"Available ports: {json.dumps(list_local_ports())}")

    port = msg.get("port")
    target = find_gui_by_port(port)

    if action == "connect" and not target:
        # 1) 本机是否真的存在该串口
        if normalize_port(port) not in [normalize_port(d) for d in list_local_ports()]:
            return await reply(mcp_cid, req_id, False,
                               f"Serial port {port} does not exist on this computer. Available: {list_local_ports()}")
        # 2) 找空闲窗口; 没有就拉起一个新的 GUI client
        target = find_idle_gui()
        if not target:
            if not spawn_gui_client():
                return await reply(mcp_cid, req_id, False,
                                   "Could not start a new window. Please open xtell-serial manually and try again.")
            target = await wait_for_idle_gui()
            if not target:
                return await reply(mcp_cid, req_id, False,
                                   "Timed out starting a new window. Please open xtell-serial manually and try again.")

    if not target:
        return await reply(mcp_cid, req_id, False,
                           f"No window is managing {port}. Call connect_serial first.")

    if action in QUERY_TARGET:
        pending[req_id] = mcp_cid          # 记住来源, response 回来时按此回传
        await send(target["writer"], {
            "cmd": "query",
            "target": QUERY_TARGET[action],
            "lines_count": msg.get("lines_count", 50),
            "start_time": msg.get("start_time"),
            "end_time": msg.get("end_time"),
            "req_id": req_id,
        })
        return                              # 结果异步回传

    cmd = to_gui_cmd(action, msg)
    win = target.get("name", "")

    if action in ACK_ACTIONS:
        cmd["req_id"] = req_id
        fut = asyncio.get_running_loop().create_future()
        ack_futures[req_id] = fut
        try:
            await send(target["writer"], cmd)
            ack = await asyncio.wait_for(fut, ACK_TIMEOUT)
        except asyncio.TimeoutError:
            return await reply(mcp_cid, req_id, False,
                               f"The command was sent to window {win or port}, but no result arrived within {ACK_TIMEOUT}s "
                               f"(the window may be busy with a lot of serial data). Use read_received_data "
                               f"or check the window to confirm whether it took effect.")
        except Exception as e:
            return await reply(mcp_cid, req_id, False, f"Failed to deliver the command: {e}")
        finally:
            ack_futures.pop(req_id, None)
        text = ack.get("text", "")
        where = f"{port}" + (f" (window: {win})" if win else "")
        return await reply(mcp_cid, req_id, ack.get("ok", False),
                           f"{where}: {text}" if text else ok_text(action, port, msg, win))

    try:
        await send(target["writer"], cmd)
    except Exception as e:
        return await reply(mcp_cid, req_id, False, f"Failed to deliver the command: {e}")
    await reply(mcp_cid, req_id, True, ok_text(action, port, msg, win))


async def route_response_back(msg):
    """GUI client 的查询结果 -> 发起请求的那个 MCP server 连接。"""
    req_id = msg.get("req_id")
    cid = pending.pop(req_id, None)
    if not cid:
        return
    data = msg.get("data", [])
    await reply(cid, req_id, True,
                "\n".join(data) if data else "No matching data found in the buffer.")


###############################################################################
# 连接处理
###############################################################################

async def handle_client(reader, writer):
    cid = str(uuid.uuid4())
    clients[cid] = {"writer": writer, "role": "gui", "port": "", "state": "idle", "name": ""}
    log(f"client connected: {cid} from {writer.get_extra_info('peername')}")

    try:
        await send(writer, {"cmd": "hello_ack", "srv": SRV_TAG, "version": SRV_VERSION})
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                log(f"invalid json from {cid}: {line[:120]!r}")
                continue

            cmd = msg.get("cmd")
            if cmd == "hello":
                clients[cid]["role"] = msg.get("role", "gui")
                clients[cid]["name"] = msg.get("name", "") or msg.get("client", "")
                log(f"{cid} -> role={clients[cid]['role']} name={clients[cid]['name']}")
            elif cmd == "status":
                clients[cid]["port"] = normalize_port(msg.get("port", ""))
                clients[cid]["state"] = msg.get("state", "idle")
                if msg.get("name"):
                    clients[cid]["name"] = msg["name"]
            elif cmd == "response":
                await route_response_back(msg)
            elif cmd == "ack":
                fut = ack_futures.get(msg.get("req_id"))
                if fut and not fut.done():
                    fut.set_result(msg)
            elif cmd == "rpc":
                if clients[cid]["role"] != "mcp":
                    log(f"ignore rpc from non-mcp client {cid}")
                    continue
                await dispatch_rpc(cid, msg)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log(f"client {cid} error: {e}")
    finally:
        log(f"client disconnected: {cid}")
        clients.pop(cid, None)
        for rid in [k for k, v in pending.items() if v == cid]:
            pending.pop(rid, None)
        try:
            writer.close()
        except Exception:
            pass


async def _serve(sock):
    server = await asyncio.start_server(handle_client, sock=sock)
    log(f"hub listening on {HUB_HOST}:{HUB_PORT}")
    async with server:
        await server.serve_forever()


def try_become_hub(port=HUB_PORT):
    """尝试成为 Hub。

    bind 成功 -> 在后台线程里跑事件循环并返回 True;
    端口被占 -> 返回 False, 调用方回落为 client。
    监听 socket 不设 SO_REUSEADDR, 以保证 bind 的独占语义。
    """
    global _hub_thread, _is_hub

    if _is_hub and _hub_thread and _hub_thread.is_alive():
        return True

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((HUB_HOST, port))
        sock.listen(16)
        sock.setblocking(False)
    except OSError as e:
        sock.close()
        log(f"bind {port} failed ({e}); falling back to client")
        return False

    def run():
        try:
            asyncio.run(_serve(sock))
        except Exception as e:
            log(f"hub crashed: {e}")
        finally:
            global _is_hub
            _is_hub = False
            try:
                sock.close()
            except Exception:
                pass

    _hub_thread = threading.Thread(target=run, daemon=True, name="xtell-serial-hub-server")
    _hub_thread.start()
    _is_hub = True
    log("this instance is now the hub")
    return True


def is_hub():
    return _is_hub and _hub_thread is not None and _hub_thread.is_alive()
