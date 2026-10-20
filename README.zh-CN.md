# xtell-serial（极智调试串口助手）

> **关于 XTELL。** [XTELL 深圳市极智未来科技有限公司](https://www.xtell.cn/) 是一家位于深圳的嵌入式软硬件设计公司，成立于 2016 年。一支团队覆盖从电路板到云端的全部环节：PCB 设计、FPGA、固件、Linux BSP 与驱动、设备界面（HMI）、手机 App、后端与物联网平台，已交付 100 多个项目。xtell-serial 是我们为自己的调试工作做的工具，现在分享出来。联系：[sales@xtell.cn](mailto:sales@xtell.cn)。

**带图形界面、AI 助手可以通过 MCP 直接操作的串口调试工具。**
你在桌面窗口里看日志滚动，Claude、Cursor 等 MCP 客户端读取同一份日志，向同一个串口发指令。

> 状态：**早期预览（v0.1）**，目前仅支持 Windows。
>
> 已知问题：串口突然遭受大量密集数据时，窗口可能卡死，修复已列入计划。

[English](README.md)

## 功能

- 完整的串口助手：波特率最高 10Mbps，支持 utf-8 / gbk / gb2312 / ascii，HEX 显示与发送，时间戳与行号。
- 字符串或正则实时过滤，高亮，独立的过滤结果视图。
- 定时重复发送、发送历史、保存到文件（可追加）、10 万行缓冲及占用进度条。
- 中英文界面：点击菜单栏里“文件”旁边的语言项（ENG / 中文）即可切换，并会记住选择（首次运行跟随 Windows 显示语言）。
- 多窗口，每个窗口可命名，适合一台电脑接多块板子；首个窗口自动成为本机 Hub，关闭后其余窗口自动接管。
- MCP 服务端 `xtell-serial-mcp`，向 AI 客户端提供 14 个工具：列串口、连接、发送、定时发送、读取接收/发送/过滤数据（可按时间范围）、设置过滤、保存文件、清空缓冲、查询缓冲状态。

GUI 与 MCP 服务端通过本机 TCP Hub（`127.0.0.1:50016`，可用环境变量 `XTELL_SERIAL_HUB_PORT` 修改）通信，不监听公网接口。

## 从源码运行

```bash
pip install -r requirements.txt
python xtell_serial.py
```

## 打包 EXE（Windows）

```bash
pip install pyinstaller
build_exe.bat
```

产出 `dist\xtell-serial.exe`（图形界面）与 `dist\xtell-serial-mcp.exe`（MCP 服务端）。

## 接入 AI 客户端

注册给 AI 客户端的必须是 **`xtell-serial-mcp.exe`**，不是图形界面程序。完整配置与 14 个工具的参数说明见 [docs/MCP_SETUP.zh-CN.md](docs/MCP_SETUP.zh-CN.md)。

Claude Code：

```bash
claude mcp add xtell-serial C:\path\to\xtell-serial-mcp.exe
```

然后对助手说：「连上 COM3，115200，发 AT+GMR，告诉我模块返回的版本。」

## 安全提示

- AI 助手可以向端口上的设备发送任意数据，不要连接到"一条意外指令就可能造成损害"的硬件。
- 本机 Hub 只接受 `127.0.0.1` 的连接。

## 许可证

Apache License 2.0，见 [LICENSE](LICENSE)。

---

由 [XTELL 深圳市极智未来科技有限公司](https://www.xtell.cn/) 开发。
