"""Minimal UI translation layer for xtell-serial.

The Chinese string is the key. ``tr("串口:")`` returns the text in the current
language. The same table is used in reverse so that widgets which are already
on screen can be re-labelled when the user switches language.

The language is remembered in ``~/.xtell-serial/config.json``. On first run it
follows the Windows display language (Chinese -> zh, anything else -> en).
"""

import json
import os

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".xtell-serial")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

EN = {
    # window / menu
    "极智调试串口": "XTELL Serial",
    "文件": "File",
    "保存文件": "Save to File",
    "停止保存": "Stop Saving",
    "更换文件": "Change File",
    "关于": "About",
    "退出": "Exit",
    # top bar
    "串口:": "Port:",
    "刷新": "Refresh",
    "波特率:": "Baud rate:",
    "编码:": "Encoding:",
    "打开连接": "Open",
    "关闭连接": "Close",
    "[点击设置名称]": "[Click to set name]",
    # option bar
    "行号": "Line No.",
    "时间戳": "Timestamp",
    "HEX显示": "HEX view",
    "缓冲区最大行数:": "Max buffer lines:",
    "  使用:": "  Used:",
    # receive / filter / send panes
    "接收数据": "Received Data",
    "暂停滚动": "Pause scroll",
    "继续滚动": "Resume scroll",
    "清空接收": "Clear RX",
    "关键词:": "Keyword:",
    "字符串": "String",
    "正则": "Regex",
    "匹配:": "Matches:",
    "清空过滤": "Clear filter",
    "过滤结果": "Filter Results",
    "发送数据": "Send Data",
    "发送": "Send",
    "行尾:": "Line end:",
    "无": "None",
    "重复": "Repeat",
    "开始重复": "Start repeat",
    "停止重复": "Stop repeat",
    "  已发:": "  Sent:",
    "发送历史": "Send History",
    "清空历史": "Clear history",
    "  总行数:": "  Total lines:",
    # status line (templates)
    "状态: 未连接": "Status: Disconnected",
    "状态: 已连接 {port} {baud}bps": "Status: Connected {port} at {baud} bps",
    "状态: {text}": "Status: {text}",
    "警告: 端口 {port} 被其他程序占用, MCP 不可用": "Warning: port {port} is in use by another program, MCP unavailable",
    "MCP 后台保存中: {path}": "Saving in the background (MCP): {path}",
    "实时保存到: {path}": "Saving live to: {path}",
    "已停止保存: {path}": "Stopped saving: {path}",
    "保存文件写入失败": "Writing to the save file failed",
    "发送失败": "Send failed",
    # messages
    "请输入有效的波特率": "Please enter a valid baud rate",
    "请选择串口": "Please select a serial port",
    "连接失败: {err}": "Connection failed: {err}",
    "正则错误: {err}": "Regex error: {err}",
    "错误": "Error",
    "提示": "Notice",
    "连接未打开": "The connection is not open",
    "打开文件失败: {err}": "Could not open the file: {err}",
    # dialogs
    "设置名称": "Set Name",
    "请输入此窗口的自定义名称\n（用于区分多个串口助手实例）:": "Enter a name for this window\n(to tell several instances apart):",
    "选择保存文件": "Choose a file to save to",
    "文本文件": "Text files",
    "日志文件": "Log files",
    "所有文件": "All files",
    "文件已有内容": "The file is not empty",
    "文件 {name} 已有内容。\n\n点击「是」覆盖原内容\n点击「否」追加到末尾\n点击「取消」放弃":
        "The file {name} already has content.\n\nYes: overwrite it\nNo: append to the end\nCancel: do nothing",
    # about window
    "• 专业级串口调试与日志分析工具": "• Serial debugging and log analysis",
    "• 支持 MCP 协议，可对接 AI 大模型": "• MCP support: let AI assistants drive the port",
    "• 自带 Master Hub，跨进程多实例协同": "• Built-in hub: several windows work together",
    "• 支持正则表达式实时过滤与高亮": "• Live regex filtering and highlighting",
    "• 毫秒级时间戳与百万级行缓冲区": "• Millisecond timestamps, large line buffer",
    "深圳市极智未来科技有限公司": "Shenzhen XTELL Future Technology Co., Ltd.",
    "确定": "OK",
    # tray / context menu
    "显示窗口": "Show window",
    "XTELL Serial 串口助手": "XTELL Serial",
    "全选": "Select all",
    "复制": "Copy",
    "清空": "Clear",
}

REV = {v: k for k, v in EN.items()}

_lang = "zh"


def detect_system_lang():
    """Chinese Windows UI -> 'zh', anything else -> 'en'."""
    try:
        import ctypes
        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return "zh" if (lang_id & 0x3FF) == 0x04 else "en"
    except Exception:
        return "en"


def load_lang():
    """Initialise the language from the saved config, else from the system."""
    global _lang
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            value = json.load(f).get("lang")
        if value in ("zh", "en"):
            _lang = value
            return _lang
    except Exception:
        pass
    _lang = detect_system_lang()
    return _lang


def save_lang():
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"lang": _lang}, f)
    except Exception:
        pass


def get_lang():
    return _lang


def set_lang(lang):
    global _lang
    if lang in ("zh", "en"):
        _lang = lang


def tr(text, **kw):
    """Translate ``text`` (a Chinese key) into the current language."""
    out = EN.get(text, text) if _lang == "en" else text
    return out.format(**kw) if kw else out


def en(text, **kw):
    """Always English. Used for messages that are returned to AI clients."""
    out = EN.get(text, text)
    return out.format(**kw) if kw else out


def convert(text):
    """Map a string that may be in either language to the current language."""
    if _lang == "en":
        return EN.get(text, text)
    return REV.get(text, text)
