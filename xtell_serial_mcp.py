"""xtell-serial MCP server —— 薄适配层(client 角色)。

由 Claude Code / Claude Desktop / Antigravity 通过 stdio 拉起, 自身不含界面、不占用任何端口。
它连接 Hub(第一个 GUI), 把工具调用翻译成 rpc 报文, 再把结果翻译回 TextContent。

铁律:
  1. stdout 只跑 JSON-RPC, 所有日志写 stderr;
  2. initialize / tools/list 不连 Hub, 必须秒回;
  3. 仅 tools/call 时懒连接 Hub, 失败返回可读文案而不是抛异常。
"""

import asyncio
import json
import os
import sys
import time
import uuid

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

HUB_HOST = "127.0.0.1"
HUB_PORT = int(os.environ.get("XTELL_SERIAL_HUB_PORT", "50016"))
CONNECT_TIMEOUT = 1.0
RPC_TIMEOUT = 20.0
SRV_TAG = "xtell_serial_hub"

NO_HUB_HINT = (f"未检测到运行中的 xtell-serial (Hub 未监听 {HUB_HOST}:{HUB_PORT})。\n"
               "请先打开 xtell-serial 程序后重试。")


def log(msg):
    try:
        sys.stderr.write(f"[xtell-serial-mcp] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


NO_STDIO_MSG = (
    "xtell-serial-mcp 是 MCP stdio 服务端, 只能由 Claude Code / Claude Desktop / Antigravity 等客户端\n"
    "通过管道拉起, 不能直接双击运行。\n\n"
    "请把它注册到客户端配置的 mcpServers 里:\n"
    '  "xtell-serial": {\n'
    '      "command": "<本程序的完整路径>",\n'
    '      "args": []\n'
    "  }\n\n"
    "然后重启客户端。串口界面请运行同目录下的 xtell-serial.exe。"
)


def require_stdio():
    """没有可用的 stdin/stdout 时给出人话提示, 而不是抛一串 traceback。"""
    if sys.stdin is not None and sys.stdout is not None:
        return

    try:
        logfile = os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                               "xtell-serial-mcp_error.log")
        with open(logfile, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 没有 stdin/stdout, 拒绝启动\n")
    except Exception:
        pass

    log(NO_STDIO_MSG)

    try:
        import ctypes
        if sys.stderr is None and ctypes.windll.kernel32.GetConsoleWindow() == 0:
            ctypes.windll.user32.MessageBoxW(0, NO_STDIO_MSG, "xtell-serial MCP Server", 0x30)
    except Exception:
        pass

    raise SystemExit(2)


###############################################################################
# Hub 连接(懒连接 + 自动重连)
###############################################################################

class HubLink:
    def __init__(self):
        self.reader = None
        self.writer = None
        self.pending = {}          # req_id -> Future
        self.lock = asyncio.Lock()

    def _alive(self):
        return self.writer is not None and not self.writer.is_closing()

    async def ensure(self):
        if self._alive():
            return True
        async with self.lock:
            if self._alive():
                return True
            try:
                self.reader, self.writer = await asyncio.wait_for(
                    asyncio.open_connection(HUB_HOST, HUB_PORT), CONNECT_TIMEOUT)
            except Exception as e:
                log(f"connect hub failed: {e}")
                self.reader = self.writer = None
                return False
            await self._send({
                "cmd": "hello",
                "role": "mcp",
                "client": os.environ.get("MCP_CLIENT_NAME", "claude"),
                "pid": os.getpid(),
            })
            asyncio.create_task(self._reader_loop())
            log(f"connected to hub {HUB_HOST}:{HUB_PORT}")
            return True

    async def _send(self, obj):
        self.writer.write((json.dumps(obj) + "\n").encode("utf-8"))
        await self.writer.drain()

    async def _reader_loop(self):
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8").strip())
                except json.JSONDecodeError:
                    continue
                if msg.get("cmd") == "hello_ack":
                    if msg.get("srv") != SRV_TAG:
                        log(f"端口 {HUB_PORT} 被无关程序占用: {msg}")
                elif msg.get("cmd") == "rpc_result":
                    fut = self.pending.pop(msg.get("req_id"), None)
                    if fut and not fut.done():
                        fut.set_result(msg)
        except Exception as e:
            log(f"hub link closed: {e}")
        finally:
            self.reader = self.writer = None
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("hub disconnected"))
            self.pending.clear()

    async def rpc(self, action, **params):
        """所有工具调用的唯一出口; 失败一律返回可读文案, 绝不抛给 Claude。"""
        if not await self.ensure():
            return NO_HUB_HINT

        req_id = str(uuid.uuid4())
        fut = asyncio.get_running_loop().create_future()
        self.pending[req_id] = fut
        try:
            await self._send({"cmd": "rpc", "req_id": req_id, "action": action, **params})
            res = await asyncio.wait_for(fut, RPC_TIMEOUT)
            return res.get("text", "")
        except asyncio.TimeoutError:
            return f"Hub 在 {RPC_TIMEOUT}s 内未返回 {action} 的结果, 请检查目标窗口是否卡死。"
        except ConnectionError:
            return "与 Hub 的连接已断开(可能是主窗口被关闭), 请稍后重试。"
        except Exception as e:
            return f"与 Hub 通信失败: {e}"
        finally:
            self.pending.pop(req_id, None)


hub = HubLink()

###############################################################################
# MCP 服务端
###############################################################################

mcp_server = Server("xtell-serial-mcp")


@mcp_server.list_tools()
async def list_tools():
    """工具清单为静态文本, 此处禁止任何 TCP 调用(必须秒回)。"""
    return [
        types.Tool(
            name="list_serial_ports",
            description="扫描电脑物理串口，返回可用串口列表。",
            inputSchema={
                "type": "object",
                "properties": {},
            }
        ),
        types.Tool(
            name="connect_serial",
            description="控制连接特定串口。如果所有已有窗口都在使用其他串口，会自动拉起新窗口连上此串口。",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string", "description": "要连接的串口号，例如 COM3 或 /dev/ttyUSB0"},
                    "baudrate": {"type": "integer", "description": "波特率，例如 115200"},
                },
                "required": ["port", "baudrate"]
            }
        ),
        types.Tool(
            name="send_serial_data",
            description="控制往特定串口发送单条数据。必须确保已经 connect_serial 连上了该串口。",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string", "description": "目标串口号"},
                    "data": {"type": "string", "description": "要发送的文本或 HEX 字符串"},
                    "hex_mode": {"type": "boolean", "description": "是否以 HEX 十六进制模式发送", "default": False},
                },
                "required": ["port", "data"]
            }
        ),
        types.Tool(
            name="disconnect_serial",
            description="控制特定串口断开连接。",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string", "description": "目标串口号"}
                },
                "required": ["port"]
            }
        ),
        types.Tool(
            name="start_auto_send",
            description="开启 GUI 定时自动发送数据。",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string"},
                    "data": {"type": "string", "description": "要发送的数据"},
                    "interval_ms": {"type": "integer", "description": "时间间隔(毫秒)", "default": 1000},
                    "hex_mode": {"type": "boolean", "default": False}
                },
                "required": ["port", "data", "interval_ms"]
            }
        ),
        types.Tool(
            name="stop_auto_send",
            description="停止 GUI 定时自动发送。",
            inputSchema={
                "type": "object",
                "properties": {"port": {"type": "string"}},
                "required": ["port"]
            }
        ),
        types.Tool(
            name="set_filter",
            description="设置 GUI 上的过滤条件。",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string"},
                    "keyword": {"type": "string"},
                    "is_regex": {"type": "boolean", "default": False},
                    "enable": {"type": "boolean", "default": True}
                },
                "required": ["port", "keyword"]
            }
        ),
        types.Tool(
            name="start_saving_file",
            description="开启后台将串口数据保存至本地文件。文件必须是全路径。",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string"},
                    "filepath": {"type": "string", "description": "绝对路径文件, 例 C:/log.txt"}
                },
                "required": ["port", "filepath"]
            }
        ),
        types.Tool(
            name="stop_saving_file",
            description="停止保存并关闭文件。",
            inputSchema={
                "type": "object",
                "properties": {"port": {"type": "string"}},
                "required": ["port"]
            }
        ),
        types.Tool(
            name="clear_data_buffers",
            description="清空 GUI 界面上的数据文本。",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string"},
                    "target": {"type": "string", "description": "main, filter, history, or all", "default": "all"}
                },
                "required": ["port"]
            }
        ),
        types.Tool(
            name="read_received_data",
            description="抓取串口主接收窗口的数据记录（最后 N 行）。可以传入起止时间戳来过滤（格式: yyyy-mm-dd HH:MM:SS.mmm）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string"},
                    "lines_count": {"type": "integer", "default": 50},
                    "start_time": {"type": "string", "description": "可选起点时间戳"},
                    "end_time": {"type": "string", "description": "可选终点时间戳"}
                },
                "required": ["port"]
            }
        ),
        types.Tool(
            name="read_sent_data",
            description="抓取串口发送历史的数据记录（最后 N 行）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string"},
                    "lines_count": {"type": "integer", "default": 50},
                    "start_time": {"type": "string", "description": "可选起点时间戳"},
                    "end_time": {"type": "string", "description": "可选终点时间戳"}
                },
                "required": ["port"]
            }
        ),
        types.Tool(
            name="read_filtered_data",
            description="抓取命中过滤规则的数据记录（最后 N 行）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string"},
                    "lines_count": {"type": "integer", "default": 50},
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"}
                },
                "required": ["port"]
            }
        ),
        types.Tool(
            name="get_buffer_status",
            description="查询指定串口当前窗口的缓冲区行数使用情况与占用百分比。",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string", "description": "目标串口号"}
                },
                "required": ["port"]
            }
        )
    ]


ACTION_MAP = {
    "list_serial_ports":  "list_ports",
    "connect_serial":     "connect",
    "disconnect_serial":  "disconnect",
    "send_serial_data":   "send",
    "start_auto_send":    "auto_send_on",
    "stop_auto_send":     "auto_send_off",
    "set_filter":         "set_filter",
    "start_saving_file":  "save_on",
    "stop_saving_file":   "save_off",
    "clear_data_buffers": "clear",
    "read_received_data": "query_main",
    "read_sent_data":     "query_sent",
    "read_filtered_data": "query_filter",
    "get_buffer_status":  "get_buffer_status",
}


def local_ports_text():
    """Hub 不可达时的兜底: 串口枚举本来就不需要 GUI。"""
    try:
        import serial.tools.list_ports
        devs = [p.device for p in serial.tools.list_ports.comports()]
        return (f"Available ports: {json.dumps(devs)}\n"
                f"(注意: {NO_HUB_HINT} 上述列表由 MCP 适配层直接枚举得到。)")
    except Exception as e:
        return f"{NO_HUB_HINT} 本地枚举串口也失败了: {e}"


@mcp_server.call_tool()
async def handle_call_tool(name, arguments):
    action = ACTION_MAP.get(name)
    if not action:
        raise ValueError(f"Unknown tool: {name}")

    text = await hub.rpc(action, **(arguments or {}))
    if action == "list_ports" and text == NO_HUB_HINT:
        text = local_ports_text()
    return [types.TextContent(type="text", text=text)]


async def main():
    log(f"starting (hub={HUB_HOST}:{HUB_PORT})")
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await mcp_server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="xtell-serial-mcp",
                server_version="2.0.0",
                capabilities=mcp_server.get_capabilities(
                    notification_options=NotificationOptions(tools_changed=True),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    require_stdio()
    asyncio.run(main())
