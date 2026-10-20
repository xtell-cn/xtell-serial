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

NO_HUB_HINT = (f"No running xtell-serial was found (the hub is not listening on {HUB_HOST}:{HUB_PORT}).\n"
               "Please open the xtell-serial program and try again.")


def log(msg):
    try:
        sys.stderr.write(f"[xtell-serial-mcp] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


NO_STDIO_MSG = (
    "xtell-serial-mcp is an MCP stdio server. It can only be started by a client such as\n"
    "Claude Code, Claude Desktop or Antigravity through a pipe; it cannot be run by double-clicking.\n\n"
    "Register it under mcpServers in your client configuration:\n"
    '  "xtell-serial": {\n'
    '      "command": "<full path of this program>",\n'
    '      "args": []\n'
    "  }\n\n"
    "Then restart the client. To open the serial terminal, run xtell-serial.exe from the same folder."
)


def require_stdio():
    """没有可用的 stdin/stdout 时给出人话提示, 而不是抛一串 traceback。"""
    if sys.stdin is not None and sys.stdout is not None:
        return

    try:
        logfile = os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                               "xtell-serial-mcp_error.log")
        with open(logfile, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] no stdin/stdout available, refusing to start\n")
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
                        log(f"port {HUB_PORT} is held by an unrelated program: {msg}")
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
            return f"The hub did not return a result for {action} within {RPC_TIMEOUT}s. Check whether the target window is frozen."
        except ConnectionError:
            return "The connection to the hub was lost (the main window may have been closed). Please try again shortly."
        except Exception as e:
            return f"Communication with the hub failed: {e}"
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
            description="List the serial ports on this computer.",
            inputSchema={
                "type": "object",
                "properties": {},
            }
        ),
        types.Tool(
            name="connect_serial",
            description="Connect a serial port. If every existing window is already using another port, a new window is opened for this one.",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string", "description": "Port to connect, for example COM3 or /dev/ttyUSB0"},
                    "baudrate": {"type": "integer", "description": "Baud rate, for example 115200"},
                },
                "required": ["port", "baudrate"]
            }
        ),
        types.Tool(
            name="send_serial_data",
            description="Send one message to a serial port. The port must already be connected with connect_serial.",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string", "description": "Target port"},
                    "data": {"type": "string", "description": "Text or HEX string to send"},
                    "hex_mode": {"type": "boolean", "description": "Send as hexadecimal bytes", "default": False},
                },
                "required": ["port", "data"]
            }
        ),
        types.Tool(
            name="disconnect_serial",
            description="Disconnect a serial port.",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string", "description": "Target port"}
                },
                "required": ["port"]
            }
        ),
        types.Tool(
            name="start_auto_send",
            description="Start sending data repeatedly on a timer.",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string"},
                    "data": {"type": "string", "description": "Data to send"},
                    "interval_ms": {"type": "integer", "description": "Interval in milliseconds", "default": 1000},
                    "hex_mode": {"type": "boolean", "default": False}
                },
                "required": ["port", "data", "interval_ms"]
            }
        ),
        types.Tool(
            name="stop_auto_send",
            description="Stop the repeating send.",
            inputSchema={
                "type": "object",
                "properties": {"port": {"type": "string"}},
                "required": ["port"]
            }
        ),
        types.Tool(
            name="set_filter",
            description="Set the live filter of a window.",
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
            description="Start saving the data received on a port to a local file. The path must be absolute.",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string"},
                    "filepath": {"type": "string", "description": "Absolute file path, for example C:/log.txt"}
                },
                "required": ["port", "filepath"]
            }
        ),
        types.Tool(
            name="stop_saving_file",
            description="Stop saving and close the file.",
            inputSchema={
                "type": "object",
                "properties": {"port": {"type": "string"}},
                "required": ["port"]
            }
        ),
        types.Tool(
            name="clear_data_buffers",
            description="Clear the text buffers of a window.",
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
            description="Read the last N lines of the main receive window. Optional start and end timestamps filter the range (format: yyyy-mm-dd HH:MM:SS.mmm).",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string"},
                    "lines_count": {"type": "integer", "default": 50},
                    "start_time": {"type": "string", "description": "Optional start timestamp"},
                    "end_time": {"type": "string", "description": "Optional end timestamp"}
                },
                "required": ["port"]
            }
        ),
        types.Tool(
            name="read_sent_data",
            description="Read the last N lines of the send history.",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string"},
                    "lines_count": {"type": "integer", "default": 50},
                    "start_time": {"type": "string", "description": "Optional start timestamp"},
                    "end_time": {"type": "string", "description": "Optional end timestamp"}
                },
                "required": ["port"]
            }
        ),
        types.Tool(
            name="read_filtered_data",
            description="Read the last N lines that match the current filter.",
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
            description="Report how many lines the window buffer holds, its limit and the percentage used.",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string", "description": "Target port"}
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
                f"(Note: {NO_HUB_HINT} The list above was enumerated directly by the MCP adapter.)")
    except Exception as e:
        return f"{NO_HUB_HINT} Enumerating local ports also failed: {e}"


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
