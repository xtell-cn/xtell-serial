# xtell-serial

**A serial terminal with a GUI that your AI assistant can drive through MCP.**
You watch the log scroll in a normal desktop window; Claude, Cursor or any MCP client reads the same log and sends commands to the same port.

> Status: **early, private preview (v0.1)**. Windows only for now. English documentation is a work in progress; the full MCP tool reference is in [docs/MCP_SETUP.zh-CN.md](docs/MCP_SETUP.zh-CN.md) (Chinese).
>
> Known issue: the window can freeze when a device sends data at very high rates (for example at 2 Mbaud). A fix is planned before any public release.

[中文说明](README.zh-CN.md)

## What it is

- A full serial terminal: baud rates up to 10 Mbaud, UTF-8 / GBK / GB2312 / ASCII, HEX view and send, timestamps, line numbers.
- Live filtering by substring or regular expression, with highlighting and a separate filtered view.
- Repeat-send, send history, save-to-file with append mode, a 100,000-line buffer with a usage bar.
- Several windows at once, each with its own name, for bench setups with more than one board. The first window acts as a local hub; if it closes, another one takes over.
- An MCP server (`xtell-serial-mcp`) that exposes 14 tools to AI clients: list ports, connect, send, auto-send, read received / sent / filtered data (with optional time ranges), set a filter, save to file, clear buffers, check buffer status.

The GUI and the MCP server talk over a local TCP hub on `127.0.0.1:50016` (override with `XTELL_SERIAL_HUB_PORT`). Nothing listens on a public interface.

## Run from source

```bash
pip install -r requirements.txt
python xtell_serial.py
```

## Build executables (Windows)

```bash
pip install pyinstaller
build_exe.bat
```

This produces `dist\xtell-serial.exe` (the GUI) and `dist\xtell-serial-mcp.exe` (the MCP server).

## Connect an AI client

Register **`xtell-serial-mcp.exe`** (not the GUI) as an MCP server. For Claude Code:

```bash
claude mcp add xtell-serial C:\path\to\xtell-serial-mcp.exe
```

For Claude Desktop, Cursor and others, add this to the client's MCP configuration (see [mcp_config.example.json](mcp_config.example.json)):

```json
{
  "mcpServers": {
    "xtell-serial": {
      "command": "C:\\path\\to\\xtell-serial-mcp.exe",
      "args": []
    }
  }
}
```

Then ask your assistant something like: *"Connect to COM3 at 115200, send `AT+GMR`, and tell me what version the module reports."*

## Safety notes

- The assistant can send arbitrary bytes to whatever device is on the port. Do not connect it to hardware where an unexpected command could cause harm.
- The local hub accepts connections from `127.0.0.1` only.

## License

Apache License 2.0, see [LICENSE](LICENSE).

---

Built by [XTELL](https://www.xtell.cn/), an embedded hardware and software design house in Shenzhen.
