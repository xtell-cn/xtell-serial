import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import serial
import serial.tools.list_ports
import threading
import time
import re
import os
import sys
import socket
import json
import asyncio
import random
import traceback
from datetime import datetime
from PIL import Image
import pystray
import ctypes
import queue
from abc import ABC, abstractmethod

import hub_server
import i18n
from i18n import tr, en

def log_err(msg):
    try:
        sys.stderr.write(f"[xtell-serial] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass

# 设置 Windows 任务栏 AppUserModelID，确保图标显示正确
try:
    myappid = 'XTELL.XtellSerial.1.0'
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
except Exception:
    pass

APP_VERSION = "Build 7"
DEFAULT_MAX_LINES = 100000

# ═══════════════════════════════════════════════════════════════════════════
# 连接抽象接口与实现
# ═══════════════════════════════════════════════════════════════════════════

class IConnection(ABC):
    """统一的连接接口"""
    @property
    @abstractmethod
    def conn_id(self) -> str:
        """返回唯一连接标识，如 'COM3'"""
        pass

    @abstractmethod
    def connect(self):
        """建立连接"""
        pass

    @abstractmethod
    def disconnect(self):
        """断开连接"""
        pass

    @abstractmethod
    def write(self, data: bytes):
        """发送数据"""
        pass

    @abstractmethod
    def read_available(self) -> bytes:
        """非阻塞读取可用数据，无数据返回 b''"""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """检查连接状态"""
        pass


class SerialConnection(IConnection):
    """串口连接包装器"""
    def __init__(self, port: str, baudrate: int, timeout: float = 0.1):
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._serial = None

    @property
    def conn_id(self) -> str:
        return self._port

    def connect(self):
        self._serial = serial.Serial(self._port, self._baudrate, timeout=self._timeout)

    def disconnect(self):
        if self._serial:
            try:
                self._serial.close()
            except:
                pass
            self._serial = None

    def write(self, data: bytes):
        if self._serial:
            self._serial.write(data)

    def read_available(self) -> bytes:
        if self._serial and self._serial.in_waiting > 0:
            return self._serial.read(self._serial.in_waiting)
        return b""

    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open


def get_icon_path(size):
    """获取对应尺寸的图标路径"""
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, f"icon_{size}.png")

class ToolTip:
    """简单的鼠标悬停提示"""
    def __init__(self, widget, text=""):
        self.widget = widget
        self.text = text
        self.tip_window = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)
        # 保存引用以便更新
        widget._tooltip = self
    
    def show(self, event=None):
        if self.tip_window or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(tw, text=self.text, background="#ffffe0", relief=tk.SOLID, borderwidth=1,
                         font=("Consolas", 9), padx=4, pady=2)
        label.pack()
    
    def hide(self, event=None):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None

class SerialReceiver:
    def __init__(self, root):
        i18n.load_lang()
        self.root = root
        self.root.title(tr("极智调试串口"))
        self._status = ("状态: 未连接", "gray", {})
        self.instance_name = ""
        self.root.geometry("900x750")

        self.connection: IConnection = None
        self.running = False
        self.receive_thread = None
        self.max_lines = DEFAULT_MAX_LINES

        # 每窗口独立的暂停状态
        self.paused_main = False
        self.paused_filter = False
        self.paused_send_history = False
        
        # 按行存储: [(line_text, timestamp_str, line_number), ...]
        self.all_lines = []
        self.receive_line_counter = 0  # 接收数据行号计数器
        self.send_history_counter = 0  # 发送历史行号计数器
        self.line_buffer = ""  # 用于累积不完整的行
        self.raw_buffer = b""  # 用于累积不完整的原始bytes
        self.port_mapping = {} # 用于描述名到设备名的映射
        
        # 过滤关键字历史
        self.filter_history = []
        
        # 实时保存文件
        self.save_file_handle = None
        
        # 保存相关状态
        self.save_file = None
        self.save_filepath = ""
        
        # 重复发送
        self.repeat_sending = False
        self.repeat_job = None

        # 过滤防抖定时器
        self._filter_debounce_job = None

        # Hub 相关状态 (本实例可能是 Hub, 也可能只是 client)
        self.hub_socket = None
        self.hub_connected = False
        
        self.rx_queue = queue.Queue()
        self.rx_timer = None
        self.last_data_time = time.time()
        
        self.setup_ui()
        self._fit_button_widths()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_tray_icon()
        
        # 绑定窗口最小化事件 (Unmap 会在窗口被隐藏或最小化时触发)
        self.root.bind("<Unmap>", self._on_minimize)
        
        # 启动 Hub 连接与选举循环
        threading.Thread(target=self._hub_link_loop, daemon=True).start()
        
        # 启动时弹出关于窗口
        self.root.after(500, self._show_about)

    def _hub_send(self, obj):
        """向 Hub 发送一条 JSON 报文"""
        if not self.hub_socket or not self.hub_connected:
            return
        try:
            self.hub_socket.sendall((json.dumps(obj) + "\n").encode('utf-8'))
        except Exception:
            self.hub_connected = False

    def _hub_ack(self, req_id, ok, text):
        """把动作真实执行结果回报给 Hub, 用于转交 MCP 端的返回文本。
        没有 req_id 说明是协议不需要 ack 的动作, 直接略过。"""
        if req_id:
            self._hub_send({"cmd": "ack", "req_id": req_id, "ok": ok, "text": text})

    def _mcp_send_status(self):
        """向 Hub 上报当前 GUI 的最新状态"""
        if not self.hub_connected:
            return
        state = "connected" if self.running else "idle"
        port = self.connection.conn_id if self.running and self.connection else ""
        self._hub_send({
            "cmd": "status",
            "port": port,
            "state": state,
            "name": self.custom_name_var.get()
        })

    def _hub_link_loop(self):
        """Hub 连接与选举主循环

        先尝试连接 Hub; 连不上说明还没有 Hub, 随机抖动后尝试 bind 抢主,
        抢到就成为 Hub(本进程同时仍是一个 client), 没抢到继续当 client。
        """
        while True:
            try:
                self.hub_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                # 短超时探测: Windows 上连接没人监听的回环端口要 2s 才返回 refused
                self.hub_socket.settimeout(0.3)
                self.hub_socket.connect((hub_server.HUB_HOST, hub_server.HUB_PORT))
                self.hub_socket.settimeout(None)     # 之后的读取恢复阻塞模式
                self.hub_connected = True
                self._hub_send({"cmd": "hello", "role": "gui",
                                "name": self.custom_name_var.get(), "pid": os.getpid()})
                self._mcp_send_status()      # 重连后立即补状态
                log_err(f"connected to hub ({'self' if hub_server.is_hub() else 'remote'})")

                reader = self.hub_socket.makefile('r', encoding='utf-8')
                for line in reader:
                    if not line: break
                    try:
                        self._handle_hub_message(json.loads(line.strip()))
                    except Exception as e:
                        log_err(f"handle hub message failed: {e}")

            except (ConnectionRefusedError, socket.timeout):
                time.sleep(random.uniform(0, 0.3))   # 抖动, 避免多个 GUI 同时抢主
                if not hub_server.try_become_hub():
                    time.sleep(0.3)
                continue
            except Exception as e:
                log_err(f"hub link: {e}")
            finally:
                self.hub_connected = False
                if self.hub_socket:
                    try:
                        self.hub_socket.close()
                    except: pass
                time.sleep(2)

    def _handle_hub_message(self, msg):
        """处理来自 Hub 的 JSON 报文"""
        cmd = msg.get("cmd")
        if not cmd: return

        if cmd == "hello_ack":
            if msg.get("srv") != hub_server.SRV_TAG:
                log_err(f"port {hub_server.HUB_PORT} is held by an unrelated program: {msg}")
                self.root.after(0, lambda: self._set_status(
                    "警告: 端口 {port} 被其他程序占用, MCP 不可用", "red", port=hub_server.HUB_PORT))
        elif cmd == "connect":
            port = msg.get("port")
            baudrate = msg.get("baudrate")
            req_id = msg.get("req_id")
            self.root.after(0, lambda: self._mcp_do_connect(port, baudrate, req_id))
        elif cmd == "send":
            data = msg.get("data", "")
            hex_mode = msg.get("hex_mode", False)
            req_id = msg.get("req_id")
            self.root.after(0, lambda: self._mcp_do_send(data, hex_mode, req_id))
        elif cmd == "get_buffer_status":
            req_id = msg.get("req_id")
            self.root.after(0, lambda: self._mcp_do_get_buffer_status(req_id))
        elif cmd == "disconnect":
            self.root.after(0, self.close_connection)
        elif cmd == "auto_send":
            self.root.after(0, lambda: self._mcp_do_auto_send(msg))
        elif cmd == "filter":
            self.root.after(0, lambda: self._mcp_do_filter(msg))
        elif cmd == "save_file":
            self.root.after(0, lambda: self._mcp_do_save_file(msg))
        elif cmd == "clear":
            self.root.after(0, lambda: self._mcp_do_clear(msg.get("target")))
        elif cmd == "query":
            self.root.after(0, lambda: self._mcp_do_query(msg))

    def _mcp_do_connect(self, port, baudrate, req_id=None):
        if self.running:
            self.close_connection()
        self.port_combo.set(port)
        self.baud_combo.set(str(baudrate))
        ok, text = self._open_connection_impl(silent=True)
        self._hub_ack(req_id, ok, text)

    def _mcp_do_send(self, data, hex_mode, req_id=None):
        if not self.running or not self.connection:
            return self._hub_ack(req_id, False, "This window's connection is not open; nothing was sent")
        if not (data or "").strip():
            return self._hub_ack(req_id, False, "Nothing to send (empty data)")
        self.send_hex_var.set(hex_mode)
        self.send_text.delete(1.0, tk.END)
        self.send_text.insert(1.0, data)
        try:
            self._send_once(data.strip())
        except Exception as e:
            return self._hub_ack(req_id, False, f"Send failed: {e}")
        self._hub_ack(req_id, True, f"Sent: {data.strip()}")

    def _mcp_do_get_buffer_status(self, req_id=None):
        used = len(self.all_lines)
        max_l = self.max_lines
        rem = max(0, max_l - used)
        pct = f"{(used / max_l * 100):.2f}%" if max_l > 0 else "0%"
        text = f"Buffer status: {used} lines used, limit {max_l} lines, {rem} lines free (usage: {pct})"
        self._hub_ack(req_id, True, text)

    def _mcp_do_auto_send(self, msg):
        enable = msg.get("enable", True)
        if enable:
            if not self.connection or not self.running:
                return
            self.send_text.delete(1.0, tk.END)
            self.send_text.insert(1.0, msg.get("data", ""))
            self.send_hex_var.set(msg.get("hex_mode", False))
            self.repeat_interval_var.set(str(msg.get("interval", 1000)))
            if not self.repeat_sending:
                self._toggle_repeat_send()
        else:
            if self.repeat_sending:
                self._toggle_repeat_send()

    def _mcp_do_filter(self, msg):
        self.filter_entry.delete(0, tk.END)
        self.filter_entry.insert(0, msg.get("keyword", ""))
        self.filter_mode_var.set("regex" if msg.get("is_regex") else "text")
        if not msg.get("enable", True):
            self.filter_entry.delete(0, tk.END)
        self.on_filter_changed()

    def _mcp_do_save_file(self, msg):
        if msg.get("enable", True):
            filepath = msg.get("filepath")
            if not filepath: return
            self._close_save_file()
            try:
                self.save_file_handle = open(filepath, 'a', encoding='utf-8')
                self.save_filepath = filepath
                self.file_menu.entryconfigure(0, label=tr("更换文件"))
                self.file_menu.entryconfigure(1, state=tk.NORMAL)
                self._set_status("MCP 后台保存中: {path}", "blue", path=filepath)
            except Exception:
                pass
        else:
            self.stop_save()

    def _mcp_do_clear(self, target):
        if target in ["main", "all"]:
            self._clear_main()
        if target in ["filter", "all"]:
            self.filter_text.delete("1.0", tk.END)
        if target in ["history", "all"]:
            self.send_history.config(state=tk.NORMAL)
            self.send_history.delete("1.0", tk.END)
            self.send_history.config(state=tk.DISABLED)

    def _mcp_do_query(self, msg):
        target = msg.get("target", "main")
        lines_count = msg.get("lines_count", 50)
        start_time = msg.get("start_time")
        end_time = msg.get("end_time")
        req_id = msg.get("req_id")
        
        def time_in_range(ts, s_t, e_t):
            # 简化版字符串比较：仅取时间末尾 "HH:MM:SS.mmm" 维度
            if s_t and ts < s_t[-12:]: return False
            if e_t and ts > e_t[-12:]: return False
            return True

        result_lines = []
        if target in ["main", "filter"]:
            for line_text, ts, _ln, _hex in self.all_lines:
                if not time_in_range(ts, start_time, end_time): continue
                if target == "filter":
                    pattern = self.filter_entry.get().strip()
                    matched, _ = self._match_line(line_text, pattern)
                    if not matched: continue
                result_lines.append(f"[{ts}] {line_text.strip()}")
                
        elif target == "sent":
            history_text = self.send_history.get("1.0", tk.END).strip()
            if history_text:
                for line in history_text.split('\n'):
                    if line.startswith("[") and "]" in line:
                        ts = line[1:line.find("]")]
                        if not time_in_range(ts, start_time, end_time): continue
                    if line.strip(): result_lines.append(line)
        
        if len(result_lines) > lines_count:
            result_lines = result_lines[-lines_count:]
            
        self._hub_send({"cmd": "response", "req_id": req_id, "data": result_lines})
        
    def setup_ui(self):
        # ===== 菜单栏 =====
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label=tr("保存文件"), command=self.save_to_file)
        file_menu.add_command(label=tr("停止保存"), command=self.stop_save, state=tk.DISABLED)
        file_menu.add_separator()
        file_menu.add_command(label=tr("关于"), command=self._show_about)
        file_menu.add_separator()
        file_menu.add_command(label=tr("退出"), command=self._quit_app)
        menubar.add_cascade(label=tr("文件"), menu=file_menu)
        # language switch: shows the language you will switch TO
        menubar.add_command(label=self._lang_menu_text(), command=self._toggle_language)
        self.menubar = menubar
        self.root.config(menu=menubar)
        self.file_menu = file_menu  # 保存引用以便更新菜单状态
        
        # ===== 顶部控制栏 =====
        top_frame = ttk.Frame(self.root)
        top_frame.pack(fill=tk.X, padx=10, pady=5)

        # 串口参数容器
        self.serial_frame = ttk.Frame(top_frame)
        self.serial_frame.pack(side=tk.LEFT, padx=5)

        ttk.Label(self.serial_frame, text=tr("串口:")).pack(side=tk.LEFT)
        self.port_combo = ttk.Combobox(self.serial_frame, width=25, state="readonly", postcommand=self.refresh_ports)
        self.port_combo.pack(side=tk.LEFT, padx=5)
        self.refresh_ports()

        ttk.Button(self.serial_frame, text=tr("刷新"), command=self.refresh_ports).pack(side=tk.LEFT)

        ttk.Label(self.serial_frame, text=tr("波特率:")).pack(side=tk.LEFT, padx=(10, 0))
        self.baud_combo = ttk.Combobox(self.serial_frame, width=10, values=["4800", "9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600", "1000000", "2000000"])
        self.baud_combo.set("115200")
        self.baud_combo.pack(side=tk.LEFT, padx=5)

        ttk.Label(self.serial_frame, text=tr("编码:")).pack(side=tk.LEFT, padx=(10, 0))
        self.encoding_combo = ttk.Combobox(self.serial_frame, width=8, values=["utf-8", "gbk", "gb2312", "ascii"])
        self.encoding_combo.set("utf-8")
        self.encoding_combo.pack(side=tk.LEFT, padx=5)

        self.open_btn = ttk.Button(top_frame, text=tr("打开连接"), command=self.toggle_connection)
        self.open_btn.pack(side=tk.LEFT, padx=10)
        
        # 自定义实例名称（靠右）
        self.custom_name_var = tk.StringVar(value=tr("[点击设置名称]"))
        self.custom_name_label = tk.Label(top_frame, textvariable=self.custom_name_var, 
                                          foreground="blue", cursor="hand2", font=("Microsoft YaHei", 10, "bold"))
        self.custom_name_label.pack(side=tk.RIGHT, padx=10)
        self.custom_name_label.bind("<Button-1>", self._set_custom_name)

        # ===== 选项栏 =====
        option_frame = ttk.Frame(self.root)
        option_frame.pack(fill=tk.X, padx=10, pady=2)
        
        self.show_line_number_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(option_frame, text=tr("行号"), variable=self.show_line_number_var, command=self.refresh_all_display).pack(side=tk.LEFT)
        
        self.show_timestamp_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(option_frame, text=tr("时间戳"), variable=self.show_timestamp_var, command=self.refresh_all_display).pack(side=tk.LEFT, padx=(10, 0))
        
        self.show_hex_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(option_frame, text=tr("HEX显示"), variable=self.show_hex_var, command=self.refresh_all_display).pack(side=tk.LEFT, padx=(10, 0))
        
        ttk.Separator(option_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        
        ttk.Label(option_frame, text=tr("缓冲区最大行数:")).pack(side=tk.LEFT)
        self.max_lines_var = tk.StringVar(value=str(DEFAULT_MAX_LINES))
        max_lines_entry = ttk.Entry(option_frame, width=8, textvariable=self.max_lines_var)
        max_lines_entry.pack(side=tk.LEFT, padx=5)
        max_lines_entry.bind("<Return>", self._on_max_lines_changed)
        max_lines_entry.bind("<FocusOut>", self._on_max_lines_changed)
        
        ttk.Label(option_frame, text=tr("  使用:")).pack(side=tk.LEFT)
        self.buffer_bar = ttk.Progressbar(option_frame, length=120, mode='determinate', maximum=100)
        self.buffer_bar.pack(side=tk.LEFT, padx=3)
        self.buffer_usage_label = ttk.Label(option_frame, text=f"0/{DEFAULT_MAX_LINES} (0%)")
        self.buffer_usage_label.pack(side=tk.LEFT)
        
        # ===== 双窗格显示区 =====
        self.paned = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        self.paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        # 上方：接收数据
        main_frame = ttk.LabelFrame(self.paned, text=tr("接收数据"))
        # 小工具栏
        main_toolbar = ttk.Frame(main_frame)
        main_toolbar.pack(fill=tk.X, padx=2, pady=(2, 0))
        self.main_pause_btn = ttk.Button(main_toolbar, text=tr("暂停滚动"), width=8, command=self._toggle_pause_main)
        self.main_pause_btn.pack(side=tk.RIGHT, padx=2)
        ttk.Button(main_toolbar, text=tr("清空接收"), width=8, command=self._clear_main).pack(side=tk.RIGHT, padx=2)
        self.main_text = scrolledtext.ScrolledText(main_frame, wrap=tk.WORD, font=("Consolas", 10), exportselection=False)
        self.main_text.pack(fill=tk.BOTH, expand=True)
        self._add_text_context_menu(self.main_text)
        self.main_text.tag_config("line_number", foreground="#999999", font=("Consolas", 9))
        self.main_text.tag_config("timestamp", foreground="#6688aa", font=("Consolas", 9, "italic"))
        self.main_text.tag_config("highlight", background="yellow", foreground="red")
        self.main_text.tag_config("jump_highlight", background="#00ff00") # 绿色闪烁提示
        self.paned.add(main_frame, weight=3)
        
        # 下方：Notebook（过滤结果 + 发送数据）
        bottom_notebook = ttk.Notebook(self.paned)
        
        # --- Tab1: 过滤结果 ---
        filter_tab = ttk.Frame(bottom_notebook)
        
        # 过滤控制栏（在Tab内部）
        filter_ctrl = ttk.Frame(filter_tab)
        filter_ctrl.pack(fill=tk.X, padx=3, pady=3)
        
        ttk.Label(filter_ctrl, text=tr("关键词:")).pack(side=tk.LEFT)
        self.filter_entry = ttk.Combobox(filter_ctrl, width=23)
        self.filter_entry['values'] = []
        self.filter_entry.pack(side=tk.LEFT, padx=3)
        self.filter_entry.bind("<<ComboboxSelected>>", self.on_filter_changed)
        self.filter_entry.bind("<KeyRelease>", self.on_filter_key_release)
        self.filter_entry.bind("<FocusOut>", self.on_filter_focus_out)
        
        self.filter_mode_var = tk.StringVar(value="string")
        ttk.Radiobutton(filter_ctrl, text=tr("字符串"), variable=self.filter_mode_var, value="string", command=self.on_filter_changed).pack(side=tk.LEFT)
        ttk.Radiobutton(filter_ctrl, text=tr("正则"), variable=self.filter_mode_var, value="regex", command=self.on_filter_changed).pack(side=tk.LEFT, padx=(0, 5))
        
        ttk.Label(filter_ctrl, text=tr("匹配:")).pack(side=tk.LEFT)
        self.filter_count_label = ttk.Label(filter_ctrl, text="0")
        self.filter_count_label.pack(side=tk.LEFT)
        
        self.regex_error_label = ttk.Label(filter_ctrl, text="", foreground="red")
        self.regex_error_label.pack(side=tk.LEFT, padx=5)
        
        # 过滤窗口小工具栏
        self.filter_pause_btn = ttk.Button(filter_ctrl, text=tr("暂停滚动"), width=8, command=self._toggle_pause_filter)
        self.filter_pause_btn.pack(side=tk.RIGHT, padx=2)
        ttk.Button(filter_ctrl, text=tr("清空过滤"), width=8, command=self._clear_filter).pack(side=tk.RIGHT, padx=2)
        
        # 过滤结果文本区
        self.filter_text = scrolledtext.ScrolledText(filter_tab, wrap=tk.WORD, font=("Consolas", 10), exportselection=False)
        self.filter_text.pack(fill=tk.BOTH, expand=True)
        self._add_text_context_menu(self.filter_text)
        self.filter_text.tag_config("line_number", foreground="#999999", font=("Consolas", 9))
        self.filter_text.tag_config("highlight", background="yellow", foreground="red")
        self.filter_text.tag_config("timestamp", foreground="#6688aa", font=("Consolas", 9, "italic"))
        self.filter_text.bind("<Double-1>", self._on_filter_double_click)
        bottom_notebook.add(filter_tab, text=tr("过滤结果"))
        
        # --- Tab2: 发送数据 ---
        send_tab = ttk.Frame(bottom_notebook)
        self._setup_send_panel(send_tab)
        bottom_notebook.add(send_tab, text=tr("发送数据"))
        
        self.paned.add(bottom_notebook, weight=2)
        
        # ===== 状态栏 =====
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.status_label = ttk.Label(status_frame, text=tr("状态: 未连接"), foreground="gray")
        self.status_label.pack(side=tk.LEFT)
        
        ttk.Label(status_frame, text=tr("  总行数:")).pack(side=tk.RIGHT)
        self.line_count_label = ttk.Label(status_frame, text="0")
        self.line_count_label.pack(side=tk.RIGHT)
    
    def _setup_send_panel(self, parent):
        """构建发送数据面板"""
        # --- 发送输入区（多行） ---
        self.send_text = scrolledtext.ScrolledText(parent, wrap=tk.WORD, font=("Consolas", 10), height=4, exportselection=False)
        self.send_text.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)
        # 绑定键盘事件：Return 发送，Shift+Return 换行
        self.send_text.bind("<Return>", self._on_send_text_return)
        self.send_text.bind("<Shift-Return>", self._on_send_text_shift_return)
        
        # --- 发送控制栏 ---
        send_ctrl = ttk.Frame(parent)
        send_ctrl.pack(fill=tk.X, padx=3, pady=2)
        
        self.send_btn = ttk.Button(send_ctrl, text=tr("发送"), command=self.send_data, width=8)
        self.send_btn.pack(side=tk.LEFT, padx=3)
        
        self.send_hex_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(send_ctrl, text="HEX", variable=self.send_hex_var).pack(side=tk.LEFT, padx=3)
        
        ttk.Label(send_ctrl, text=tr("行尾:")).pack(side=tk.LEFT)
        self.line_ending_var = tk.StringVar(value="\\r\\n")
        self.le_combo = le_combo = ttk.Combobox(send_ctrl, width=5, textvariable=self.line_ending_var,
                                values=["\\r\\n", "\\n", "\\r", tr("无")], state="readonly")
        le_combo.pack(side=tk.LEFT, padx=2)
        
        ttk.Separator(send_ctrl, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=5)
        
        self.repeat_btn = ttk.Button(send_ctrl, text=tr("重复"), command=self._toggle_repeat_send, width=6)
        self.repeat_btn.pack(side=tk.LEFT, padx=2)
        
        self.repeat_interval_var = tk.StringVar(value="1000")
        ttk.Entry(send_ctrl, width=5, textvariable=self.repeat_interval_var).pack(side=tk.LEFT)
        ttk.Label(send_ctrl, text="ms").pack(side=tk.LEFT)
        
        ttk.Label(send_ctrl, text=tr("  已发:")).pack(side=tk.LEFT)
        self.send_count_label = ttk.Label(send_ctrl, text="0")
        self.send_count_label.pack(side=tk.LEFT)
        
        # --- 发送历史 ---
        hist_toolbar = ttk.Frame(parent)
        hist_toolbar.pack(fill=tk.X, padx=3)
        ttk.Label(hist_toolbar, text=tr("发送历史")).pack(side=tk.LEFT)
        self.hist_pause_btn = ttk.Button(hist_toolbar, text=tr("暂停滚动"), width=8, command=self._toggle_pause_send_history)
        self.hist_pause_btn.pack(side=tk.RIGHT, padx=2)
        ttk.Button(hist_toolbar, text=tr("清空历史"), width=8, command=self._clear_send_history).pack(side=tk.RIGHT, padx=2)
        
        self.send_history = scrolledtext.ScrolledText(parent, wrap=tk.WORD, font=("Consolas", 9),
                                                      height=3, state=tk.DISABLED, exportselection=False)
        self.send_history.pack(fill=tk.BOTH, expand=True, padx=3, pady=(0, 3))
        self.send_history.tag_config("line_number", foreground="#999999", font=("Consolas", 8))
        self.send_history.tag_config("ts", foreground="#6688aa", font=("Consolas", 8, "italic"))
        self.send_history.tag_config("sent", foreground="#2266cc")
        self.send_count = 0
        
    def refresh_ports(self):
        """刷新并获取带描述的串口列表"""
        ports = serial.tools.list_ports.comports()
        # 建立 描述 -> 设备名 的映射
        self.port_mapping = {}
        descriptions = []
        for p in ports:
            # 移除描述中原有的 (COMx) 部分，构造新格式：COMx (驱动名称)
            clean_desc = p.description.replace(f"({p.device})", "").strip()
            display_name = f"{p.device} ({clean_desc})"
            self.port_mapping[display_name] = p.device
            descriptions.append(display_name)
        
        current_val = self.port_combo.get()
        self.port_combo['values'] = descriptions
        
        if descriptions:
            # 如果之前的选择还在列表中，保留它
            if current_val in descriptions:
                self.port_combo.set(current_val)
            else:
                self.port_combo.current(0)
        else:
            self.port_combo.set("")
    
    def _set_custom_name(self, event=None):
        from tkinter import simpledialog
        current = self.custom_name_var.get()
        if current in ("[点击设置名称]", "[Set name]"):
            current = ""
        new_name = simpledialog.askstring(tr("设置名称"), tr("请输入此窗口的自定义名称\n（用于区分多个串口助手实例）:"),
                                          initialvalue=current, parent=self.root)
        if new_name is not None:
            if new_name.strip():
                self.custom_name_var.set(new_name.strip())
                self.instance_name = new_name.strip()
                self.root.title(f"{tr('极智调试串口')} - {new_name.strip()}")
                self.custom_name_label.config(font=("Microsoft YaHei", 20, "bold"), foreground="green")
            else:
                self.custom_name_var.set(tr("[点击设置名称]"))
                self.instance_name = ""
                self.root.title(tr("极智调试串口"))
                self.custom_name_label.config(font=("Microsoft YaHei", 10, "bold"), foreground="blue")
        # 名字更改后，上报给 Hub
        self._mcp_send_status()


    # ===== language =====

    @staticmethod
    def _lang_menu_text():
        return "ENG" if i18n.get_lang() == "zh" else "中文"

    def _set_status(self, template, color, **kw):
        """Show a status line and remember it so it can be re-rendered on language change."""
        self._status = (template, color, kw)
        self.status_label.config(text=tr(template, **kw), foreground=color)

    def _fit_button_widths(self):
        """Fixed-width buttons were sized for Chinese; widen them in English so the text is not cut."""
        if not hasattr(self, "_btn_widths"):
            self._btn_widths = {}
        english = i18n.get_lang() == "en"

        def walk(w):
            for c in w.winfo_children():
                if isinstance(c, ttk.Button):
                    try:
                        orig = self._btn_widths.setdefault(str(c), int(c.cget("width") or 0))
                    except (tk.TclError, ValueError):
                        orig = 0
                    if orig:
                        if english:
                            need = len(c.cget("text")) + 2
                            if c is getattr(self, "repeat_btn", None):
                                need = max(need, 14)   # "Start repeat" / "Stop repeat"
                            c.configure(width=max(orig, need))
                        else:
                            c.configure(width=orig)
                walk(c)

        walk(self.root)

    def _toggle_language(self):
        i18n.set_lang("en" if i18n.get_lang() == "zh" else "zh")
        i18n.save_lang()
        self._apply_language()

    def _apply_language(self):
        """Re-label every widget in the current language."""
        conv = i18n.convert

        def relabel(w):
            if isinstance(w, tk.Menu):
                end = w.index("end")
                if end is not None:
                    for i in range(end + 1):
                        if w.type(i) in ("command", "cascade", "checkbutton", "radiobutton"):
                            try:
                                w.entryconfigure(i, label=conv(w.entrycget(i, "label")))
                            except tk.TclError:
                                pass
            elif isinstance(w, ttk.Notebook):
                for tab in w.tabs():
                    w.tab(tab, text=conv(w.tab(tab, "text")))
            else:
                try:
                    text = w.cget("text")
                except tk.TclError:
                    text = None
                if text:
                    new = conv(text)
                    if new != text:
                        w.configure(text=new)
            for child in w.winfo_children():
                relabel(child)

        relabel(self.root)
        self._fit_button_widths()
        self.menubar.entryconfigure(self.menubar.index("end"), label=self._lang_menu_text())
        self.custom_name_var.set(conv(self.custom_name_var.get()))
        title = tr("极智调试串口")
        self.root.title(f"{title} - {self.instance_name}" if self.instance_name else title)
        template, color, kw = self._status
        self.status_label.config(text=tr(template, **kw), foreground=color)
        none_new = tr("无")
        was_none = self.line_ending_var.get() in ("无", "None")
        self.le_combo.config(values=["\\r\\n", "\\n", "\\r", none_new])
        if was_none:
            self.line_ending_var.set(none_new)
        try:
            self.tray_icon.title = tr("XTELL Serial 串口助手")
            self.tray_icon.menu = pystray.Menu(
                pystray.MenuItem(tr("显示窗口"), self._show_window, default=True),
                pystray.MenuItem(tr("退出"), self._quit_from_tray))
            self.tray_icon.update_menu()
        except Exception:
            pass

    # ===== 每窗口独立控制 =====
    
    def _toggle_pause_main(self):
        self.paused_main = not self.paused_main
        self.main_pause_btn.config(text=tr("继续滚动") if self.paused_main else "暂停滚动")
    
    def _toggle_pause_filter(self):
        self.paused_filter = not self.paused_filter
        self.filter_pause_btn.config(text=tr("继续滚动") if self.paused_filter else "暂停滚动")
    
    def _toggle_pause_send_history(self):
        self.paused_send_history = not self.paused_send_history
        self.hist_pause_btn.config(text=tr("继续滚动") if self.paused_send_history else "暂停滚动")
    
    def _clear_main(self):
        """清空接收数据区和缓冲区"""
        self.main_text.delete("1.0", tk.END)
        self.all_lines.clear()
        self.line_buffer = ""
        self.raw_buffer = b""
        self.receive_line_counter = 0
        self._update_buffer_usage()
        # 同时清空过滤结果
        self._clear_filter()
    
    def _clear_filter(self):
        """清空过滤结果区"""
        self.filter_text.delete("1.0", tk.END)
        self.filter_count_label.config(text="0")
    
    def _clear_send_history(self):
        """清空发送历史"""
        self.send_history.config(state=tk.NORMAL)
        self.send_history.delete("1.0", tk.END)
        self.send_history.config(state=tk.DISABLED)
        self.send_history_counter = 0
        
    def toggle_connection(self):
        if self.running:
            self.close_connection()
        else:
            self.open_connection()

    def open_connection(self):
        return self._open_connection_impl(silent=False)

    def _open_connection_impl(self, silent=False):
        def fail(key, **kw):
            shown = tr(key, **kw)
            if silent:
                self._set_status("状态: {text}", "red", text=shown)
            else:
                messagebox.showerror(tr("错误"), shown)
            return False, en(key, **kw)

        self.encoding = self.encoding_combo.get()
        try:
            selected_desc = self.port_combo.get()
            port = self.port_mapping.get(selected_desc, selected_desc)
            baudrate_str = self.baud_combo.get()
            try:
                baudrate = int(baudrate_str)
                if baudrate <= 0:
                    raise ValueError()
            except ValueError:
                return fail("请输入有效的波特率")

            if not port:
                return fail("请选择串口")

            self.connection = SerialConnection(port, baudrate, timeout=0.1)
            self.connection.connect()
            self.running = True
            self.open_btn.config(text=tr("关闭连接"))
            self._set_status("状态: 已连接 {port} {baud}bps", "green", port=port, baud=baudrate)

            self.receive_thread = threading.Thread(target=self.receive_data, daemon=True)
            self.receive_thread.start()
            self._process_rx_queue()
            self._mcp_send_status()
            return True, f"Connected to {port} at {baudrate} bps"
        except Exception as e:
            self.connection = None
            self.running = False
            return fail("连接失败: {err}", err=str(e))

    def close_connection(self):
        self.running = False
        if self.rx_timer:
            self.root.after_cancel(self.rx_timer)
            self.rx_timer = None
        if self.connection:
            try:
                self.connection.disconnect()
            except:
                pass
            self.connection = None
        self.open_btn.config(text=tr("打开连接"))
        self._set_status("状态: 未连接", "gray")
        self._mcp_send_status()

    def receive_data(self):
        """后台线程：持续接收串口数据"""
        while self.running:
            try:
                if self.connection:
                    data = self.connection.read_available()
                    if data:
                        self.rx_queue.put(data)
                        self.last_data_time = time.time()
            except Exception:
                break
            time.sleep(0.01)
            
    def _process_rx_queue(self):
        if not self.running:
            return
        
        data_chunks = []
        while True:
            try:
                data_chunks.append(self.rx_queue.get_nowait())
            except queue.Empty:
                break
                
        if data_chunks:
            combined_data = b''.join(data_chunks)
            self.process_received_data(combined_data)
            self._timeout_flush()
            
        self.rx_timer = self.root.after(50, self._process_rx_queue)

    def _on_max_lines_changed(self, event=None):
        """最大行数输入变化时更新"""
        try:
            val = int(self.max_lines_var.get())
            if val < 100:
                val = 100
            self.max_lines = val
        except ValueError:
            self.max_lines_var.set(str(self.max_lines))
        self._trim_buffer()
        self._update_buffer_usage()
    
    def process_received_data(self, data):
        """处理接收到的原始数据，按行分割并存储"""
        try:
            text = data.decode(self.encoding, errors='replace')
        except Exception:
            text = data.hex()

        # 同步累积原始bytes
        self.raw_buffer += data

        # 将数据累积到行缓冲区，按换行符分割成行
        self.line_buffer += text
        
        while '\n' in self.line_buffer:
            line, self.line_buffer = self.line_buffer.split('\n', 1)
            line_bytes_len = len(line.encode(self.encoding, errors='replace')) + 1
            if self.raw_buffer:
                raw_part = self.raw_buffer[:line_bytes_len]
                self.raw_buffer = self.raw_buffer[line_bytes_len:]
            else:
                raw_part = b""
            
            complete_line = line + '\n'
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            self.receive_line_counter += 1
            line_num = self.receive_line_counter
            hex_str = raw_part.hex()
            self.all_lines.append((complete_line, timestamp, line_num, hex_str))
            self._append_line_to_main(complete_line, timestamp, line_num, hex_str)
            self._try_append_to_filter(complete_line, timestamp, line_num)
            self._write_line_to_file(complete_line, timestamp)
        
        # 缓冲区超限时裁剪
        self._trim_buffer()
        self._update_buffer_usage()
    
    def _trim_buffer(self):
        """裁剪缓冲区到最大行数限制"""
        if len(self.all_lines) > self.max_lines:
            trim_count = len(self.all_lines) - self.max_lines
            self.all_lines = self.all_lines[trim_count:]
            # 仅删除顶部的行来反映裁剪，避免全量刷新导致卡死
            self.main_text.delete("1.0", f"{trim_count + 1}.0")
            
            # 过滤区也简单截断（如果有）
            try:
                filter_lines = int(self.filter_text.index('end-1c').split('.')[0])
                if filter_lines > self.max_lines:
                    self.filter_text.delete("1.0", f"{filter_lines - self.max_lines + 1}.0")
            except Exception:
                pass
    
    def _update_buffer_usage(self):
        """更新缓冲区使用量显示"""
        current = len(self.all_lines)
        max_l = self.max_lines
        pct = min(100, int(current / max_l * 100)) if max_l > 0 else 0
        self.buffer_bar['value'] = pct
        self.buffer_usage_label.config(text=f"{current}/{max_l} ({pct}%)")
        self.line_count_label.config(text=str(current))
    
    def _timeout_flush(self):
        """超时100ms强制输出缓冲区剩余数据"""
        current_time = time.time()
        if self.line_buffer and (current_time - self.last_data_time) >= 0.1:
            remaining = self.line_buffer
            if remaining:
                timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                self.receive_line_counter += 1
                line_num = self.receive_line_counter
                hex_str = self.raw_buffer.hex() if self.raw_buffer else ""
                self.all_lines.append((remaining, timestamp, line_num, hex_str))
                self._append_line_to_main(remaining, timestamp, line_num, hex_str)
                self._try_append_to_filter(remaining, timestamp, line_num)
                self._write_line_to_file(remaining, timestamp)
                self.line_buffer = ""
                self.raw_buffer = b""
                self._update_buffer_usage()
    
    def _get_timestamp_prefix(self, timestamp):
        """返回时间戳前缀字符串（如果启用）"""
        if self.show_timestamp_var.get():
            return f"[{timestamp}] "
        return ""
    
    def _apply_highlight_to_range(self, widget, start_index, end_index, match_positions):
        """对指定范围内的匹配位置应用高亮标签"""
        # 注意：match_positions 是相对于 line 字符串开头的偏移
        # 我们需要将其转换为 Tkinter 的 index (e.g. "line.char")
        base_idx = widget.index(start_index)
        line_num = base_idx.split('.')[0]
        # 如果有时间戳前缀，偏移量需要增加前缀长度
        prefix_len = len(self._get_timestamp_prefix("")) # 这里逻辑有点绕，直接传 line 进来更稳
        
    def _insert_with_timestamp(self, text_widget, line, timestamp, highlight_pattern=None, line_num=None, hex_str=""):
        """插入一行，行号+时间戳用特殊样式，并根据 pattern 应用高亮"""
        if self.show_hex_var.get() and hex_str:
            display_text = hex_str
        else:
            display_text = line
        
        prefix = self._get_timestamp_prefix(timestamp)
        start_mark = text_widget.index(tk.END + "-1c")
        
        # 插入行号
        if line_num is not None and self.show_line_number_var.get():
            text_widget.insert(tk.END, f"{line_num:>6} ", "line_number")
        
        if prefix:
            text_widget.insert(tk.END, prefix, "timestamp")
        
        # 记录行文本开始的位置
        line_start_idx = text_widget.index(tk.END + "-1c")
        text_widget.insert(tk.END, display_text)
        line_end_idx = text_widget.index(tk.END + "-1c")
        
        # 如果有高亮模式，应用高亮
        if highlight_pattern:
            matched, positions = self._match_line(line, highlight_pattern)
            if matched:
                row_base = line_start_idx.split('.')[0]
                col_offset = int(line_start_idx.split('.')[1])
                for s, e in positions:
                    text_widget.tag_add("highlight", f"{row_base}.{col_offset + s}", f"{row_base}.{col_offset + e}")

    def _append_line_to_main(self, line, timestamp, line_num=None, hex_str=""):
        """追加一行到主显示区"""
        pattern = self.filter_entry.get().strip()
        self._insert_with_timestamp(self.main_text, line, timestamp, highlight_pattern=pattern, line_num=line_num, hex_str=hex_str)
        if not self.paused_main:
            self.main_text.see(tk.END)
    
    def _match_line(self, line, pattern):
        """根据当前模式匹配行，返回 (是否匹配, 匹配位置列表[(start,end),...])"""
        if not pattern:
            return False, []
        
        is_regex = self.filter_mode_var.get() == "regex"
        matches = []
        
        if is_regex:
            try:
                for m in re.finditer(pattern, line):
                    if m.start() != m.end():  # 忽略空匹配
                        matches.append((m.start(), m.end()))
                self.regex_error_label.config(text="")
            except re.error as e:
                self.regex_error_label.config(text=tr("正则错误: {err}", err=e))
                return False, []
        else:
            start = 0
            while True:
                idx = line.find(pattern, start)
                if idx == -1:
                    break
                matches.append((idx, idx + len(pattern)))
                start = idx + len(pattern)
        
        return len(matches) > 0, matches
    
    def _try_append_to_filter(self, line, timestamp="", line_num=None):
        """如果行匹配过滤条件，追加到过滤结果区"""
        pattern = self.filter_entry.get().strip()
        if not pattern:
            return
        
        matched, positions = self._match_line(line, pattern)
        if matched:
            self._insert_filter_line(line, timestamp, positions, line_num=line_num)
            if not self.paused_filter:
                self.filter_text.see(tk.END)
            current = int(self.filter_count_label.cget("text"))
            self.filter_count_label.config(text=str(current + 1))
    
    def _insert_filter_line(self, line, timestamp, match_positions, line_num=None):
        """插入一行到过滤区：行号+时间戳用特殊颜色，匹配内容高亮"""
        # 插入行号
        if line_num is not None and self.show_line_number_var.get():
            self.filter_text.insert(tk.END, f"{line_num:>6} ", "line_number")
        prefix = self._get_timestamp_prefix(timestamp)
        if prefix:
            self.filter_text.insert(tk.END, prefix, "timestamp")
        # 按匹配位置分段插入，高亮匹配部分
        prev_end = 0
        for start, end in match_positions:
            if start > prev_end:
                self.filter_text.insert(tk.END, line[prev_end:start])
            self.filter_text.insert(tk.END, line[start:end], "highlight")
            prev_end = end
        if prev_end < len(line):
            self.filter_text.insert(tk.END, line[prev_end:])
    
    def on_filter_changed(self, event=None):
        """过滤关键词或模式变化时，重新扫描所有已存储的行"""
        pattern = self.filter_entry.get().strip()
        self.regex_error_label.config(text="")
        
        # 清空过滤结果区
        self.filter_text.delete("1.0", tk.END)
        
        if not pattern:
            self.filter_count_label.config(text="0")
            return
        
        # 重新扫描所有行
        count = 0
        for line_text, timestamp, line_num, hex_str in self.all_lines:
            matched, positions = self._match_line(line_text, pattern)
            if matched:
                self._insert_filter_line(line_text, timestamp, positions, line_num=line_num)
                count += 1
        
        self.filter_count_label.config(text=str(count))
        if not self.paused_filter:
            self.filter_text.see(tk.END)
            
        # 同步刷新主界面的高亮
        self._refresh_main_highlights(pattern)
    
    def _refresh_main_highlights(self, pattern):
        """刷新主界面的高亮标签"""
        self.main_text.tag_remove("highlight", "1.0", tk.END)
        if not pattern:
            return
            
        # 使用 search 循环查找所有匹配项
        start_pos = "1.0"
        is_regex = self.filter_mode_var.get() == "regex"
        
        while True:
            start_pos = self.main_text.search(pattern, start_pos, stopindex=tk.END, regexp=is_regex)
            if not start_pos:
                break
            
            # 找到匹配，获取结束位置
            # 对于正则，我们需要算出匹配长度。Tkinter search 对于正则不直接返回长度，
            # 需要配合 -count 变量
            count_var = tk.IntVar()
            self.main_text.search(pattern, start_pos, stopindex=tk.END, regexp=is_regex, count=count_var)
            match_len = count_var.get()
            
            end_pos = f"{start_pos}+{match_len}c"
            self.main_text.tag_add("highlight", start_pos, end_pos)
            start_pos = end_pos

    def refresh_all_display(self):
        """刷新主显示区（时间戳/HEX切换时）"""
        self.main_text.delete("1.0", tk.END)
        # 批量重新插入，并应用当前高亮
        pattern = self.filter_entry.get().strip()
        for line_text, timestamp, line_num, hex_str in self.all_lines:
            self._insert_with_timestamp(self.main_text, line_text, timestamp, highlight_pattern=pattern, line_num=line_num, hex_str=hex_str)
            
        if not self.paused_main:
            self.main_text.see(tk.END)
        # 同时刷新过滤区内容
        self.on_filter_changed()
        
    def clear_display(self):
        """清空所有数据"""
        self._clear_main()
    
    def save_to_file(self):
        """选择文件并开始实时保存，或更换保存文件"""
        filepath = filedialog.asksaveasfilename(
            title=tr("选择保存文件"),
            defaultextension=".txt",
            filetypes=[(tr("文本文件"), "*.txt"), (tr("日志文件"), "*.log"), (tr("所有文件"), "*.*")]
        )
        if not filepath:
            return
        
        # 判断写入模式
        write_mode = 'a'  # 默认追加
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            result = messagebox.askyesnocancel(
                tr("文件已有内容"),
                tr("文件 {name} 已有内容。\n\n点击「是」覆盖原内容\n点击「否」追加到末尾\n点击「取消」放弃",
                   name=os.path.basename(filepath))
            )
            if result is None:  # 取消
                return
            elif result:  # 是 = 覆盖
                write_mode = 'w'
            # 否 = 追加，保持 'a'
        
        # 关闭之前的文件
        self._close_save_file()
        
        try:
            self.save_file_handle = open(filepath, write_mode, encoding='utf-8')
            self.save_filepath = filepath
            self.file_menu.entryconfigure(0, label=tr("更换文件"))
            self.file_menu.entryconfigure(1, state=tk.NORMAL)
            self._set_status("实时保存到: {path}", "blue", path=filepath)
        except Exception as e:
            messagebox.showerror(tr("错误"), tr("打开文件失败: {err}", err=str(e)))
    
    def stop_save(self):
        """停止实时保存"""
        self._close_save_file()
        self.file_menu.entryconfigure(0, label=tr("保存文件"))
        self.file_menu.entryconfigure(1, state=tk.DISABLED)
        self._set_status("已停止保存: {path}", "gray", path=self.save_filepath)
        self.save_filepath = ""
    
    def _close_save_file(self):
        """关闭当前保存文件句柄"""
        if self.save_file_handle:
            try:
                self.save_file_handle.flush()
                self.save_file_handle.close()
            except Exception:
                pass
            self.save_file_handle = None
    
    def _write_line_to_file(self, line, timestamp):
        """实时写一行到文件"""
        if self.save_file_handle:
            try:
                if self.show_timestamp_var.get():
                    self.save_file_handle.write(f"[{timestamp}] {line}")
                else:
                    self.save_file_handle.write(line)
                self.save_file_handle.flush()
            except Exception:
                self._close_save_file()
                self.file_menu.entryconfigure(0, label=tr("保存文件"))
                self.file_menu.entryconfigure(1, state=tk.DISABLED)
                self._set_status("保存文件写入失败", "red")
    def _show_about(self):
        """显示关于对话框"""
        import webbrowser
        about_win = tk.Toplevel(self.root)
        about_win.title(tr("关于"))
        about_win.geometry("400x320")
        about_win.resizable(False, False)
        about_win.transient(self.root)
        
        # 居中显示逻辑
        self.root.update_idletasks()
        main_x = self.root.winfo_x()
        main_y = self.root.winfo_y()
        main_w = self.root.winfo_width()
        main_h = self.root.winfo_height()
        
        # 如果主窗口还没完全显示（例如启动瞬间），则使用屏幕中心
        if main_w <= 1 or main_h <= 1:
             sw = self.root.winfo_screenwidth()
             sh = self.root.winfo_screenheight()
             x = (sw - 400) // 2
             y = (sh - 320) // 2
        else:
             x = main_x + (main_w - 400) // 2
             y = main_y + (main_h - 320) // 2
        
        about_win.geometry(f"+{x}+{y}")
        about_win.grab_set()
        
        ttk.Label(about_win, text=tr("极智调试串口"), font=("Microsoft YaHei", 16, "bold")).pack(pady=(15, 5))
        ttk.Label(about_win, text=APP_VERSION, font=("Consolas", 10)).pack()
        ttk.Separator(about_win, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=30, pady=8)
        
        # 软件功能简述
        info_frame = ttk.Frame(about_win)
        info_frame.pack(fill=tk.BOTH, expand=True, padx=30)
        
        features = [
            tr("• 专业级串口调试与日志分析工具"),
            tr("• 支持 MCP 协议，可对接 AI 大模型"),
            tr("• 自带 Master Hub，跨进程多实例协同"),
            tr("• 支持正则表达式实时过滤与高亮"),
            tr("• 毫秒级时间戳与百万级行缓冲区")
        ]
        
        for feat in features:
            ttk.Label(info_frame, text=feat, font=("Microsoft YaHei", 9)).pack(anchor="w", pady=1)

        ttk.Label(about_win, text=tr("深圳市极智未来科技有限公司"), font=("Microsoft YaHei", 10, "bold")).pack(pady=(10, 0))
        
        link = ttk.Label(about_win, text="https://www.xtell.cn/", foreground="blue", cursor="hand2", font=("Consolas", 10))
        link.pack(pady=2)
        link.bind("<Button-1>", lambda e: webbrowser.open("https://www.xtell.cn/"))
        
        ttk.Button(about_win, text=tr("确定"), command=about_win.destroy).pack(pady=10)
    
    def _on_close(self):
        """窗口关闭时直接退出程序"""
        self._quit_app()
    
    def _on_minimize(self, event):
        """当窗口由于点击最小化按钮而 Unmap 时，隐藏任务栏图标"""
        if self.root.state() == 'iconic':
            self.root.withdraw()
    
    def _quit_app(self):
        """真正退出程序"""
        self.running = False
        self.repeat_sending = False
        if self.repeat_job:
            self.root.after_cancel(self.repeat_job)
        self._close_save_file()
        if self.connection:
            try:
                self.connection.disconnect()
            except Exception:
                pass
            self.connection = None
        if hasattr(self, 'tray_icon'):
            self.tray_icon.stop()
        self.root.destroy()
    
    # ========== 发送数据 ==========

    def _on_send_text_return(self, event=None):
        """Return 键：发送数据"""
        if not self.connection or not self.running:
            return "break"

        text = self.send_text.get("1.0", tk.END).rstrip('\n')
        if text.strip():
            self._send_once(text)
            self.send_text.delete("1.0", tk.END)
        return "break"

    def _on_send_text_shift_return(self, event=None):
        """Shift+Return 键：换行（不发送）"""
        # 允许默认行为（插入换行符）
        return None

    def send_data(self):
        """发送输入框中的数据"""
        if not self.connection or not self.running:
            messagebox.showwarning(tr("提示"), tr("连接未打开"))
            return

        text = self.send_text.get("1.0", tk.END).strip()
        if not text:
            return

        self._send_once(text)

    def _send_once(self, text):
        """执行一次发送（支持转义序列和行尾处理）"""
        try:
            # 处理斜杠转义：\r\n, \n, \r, \t, \\ 等
            processed = text
            processed = processed.replace('\\r\\n', '\r\n')
            processed = processed.replace('\\n', '\n')
            processed = processed.replace('\\r', '\r')
            processed = processed.replace('\\t', '\t')
            processed = processed.replace('\\\\', '\\')

            if self.send_hex_var.get():
                # HEX 模式：去掉空格后解析十六进制
                hex_str = processed.replace(" ", "")
                data = bytes.fromhex(hex_str)
            else:
                # 文本模式：加行尾（除非用户已通过转义加了）
                if '\\' not in text or processed == text:  # 没有转义序列或转义后没变化
                    le = self.line_ending_var.get()
                    if le == "\\r\\n":
                        processed += "\r\n"
                    elif le == "\\n":
                        processed += "\n"
                    elif le == "\\r":
                        processed += "\r"
                # "无" 不加行尾
                data = processed.encode(self.encoding, errors='replace')

            self.connection.write(data)
            self.send_count += 1
            self.send_count_label.config(text=str(self.send_count))

            # 记录到发送历史
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            display = text.rstrip("\r\n")
            if self.send_hex_var.get():
                display = f"[HEX] {text}"
            self.send_history_counter += 1
            self.send_history.config(state=tk.NORMAL)
            if self.show_line_number_var.get():
                self.send_history.insert(tk.END, f"{self.send_history_counter:>4} ", "line_number")
            self.send_history.insert(tk.END, f"[{ts}] ", "ts")
            self.send_history.insert(tk.END, f"{display}\n", "sent")
            if not self.paused_send_history:
                self.send_history.see(tk.END)
            self.send_history.config(state=tk.DISABLED)

        except Exception:
            self._set_status("发送失败", "red")
    
    def _toggle_repeat_send(self):
        """切换重复发送"""
        if self.repeat_sending:
            self.repeat_sending = False
            if self.repeat_job:
                self.root.after_cancel(self.repeat_job)
                self.repeat_job = None
            self.repeat_btn.config(text=tr("开始重复"))
        else:
            if not self.connection or not self.running:
                messagebox.showwarning(tr("提示"), tr("连接未打开"))
                return
            text = self.send_text.get("1.0", tk.END).strip()
            if not text:
                return
            try:
                interval = int(self.repeat_interval_var.get())
                if interval < 10:
                    interval = 10
            except ValueError:
                interval = 1000
            self.repeat_sending = True
            self.repeat_btn.config(text=tr("停止重复"))
            self._do_repeat_send(text, interval)
    
    def _do_repeat_send(self, text, interval):
        """定时重复发送"""
        if not self.repeat_sending or not self.running:
            self.repeat_sending = False
            self.repeat_btn.config(text=tr("开始重复"))
            return
        self._send_once(text)
        self.repeat_job = self.root.after(interval, self._do_repeat_send, text, interval)
    
    def _setup_tray_icon(self):
        """创建系统托盘图标"""
        try:
            image = Image.open(get_icon_path(64))
        except Exception as e:
            print(f"failed to load the tray icon: {e}; using a default icon")
            image = Image.new('RGB', (64, 64), color='white')
        
        menu = pystray.Menu(
            pystray.MenuItem(tr("显示窗口"), self._show_window, default=True),
            pystray.MenuItem(tr("退出"), self._quit_from_tray)
        )
        
        self.tray_icon = pystray.Icon("xtell-serial", image, tr("XTELL Serial 串口助手"), menu)
        self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        self.tray_thread.start()
    
    def _show_window(self, icon=None, item=None):
        """显示窗口"""
        self.root.after(0, self._restore_window)
    
    def _restore_window(self):
        """恢复窗口显示"""
        self.root.deiconify()
        self.root.state('normal')
        self.root.attributes("-topmost", True)
        self.root.after(10, lambda: self.root.attributes("-topmost", False))
        self.root.lift()
        self.root.focus_force()
    
    def _quit_from_tray(self, icon=None, item=None):
        """从托盘退出"""
        self.root.after(0, self._quit_app)
    
    def on_filter_key_release(self, event):
        """过滤框按键释放事件，正则模式防抖2秒后触发，字符串模式立即触发，回车立即触发"""
        if event.keysym == 'Return':
            self._save_current_filter()
            self._cancel_filter_debounce()
            self.on_filter_changed()
            return
        # 取消之前的定时器
        self._cancel_filter_debounce()
        # 字符串匹配不需要防抖，立即生效；正则模式防抖2秒
        if self.filter_mode_var.get() == "regex":
            self._filter_debounce_job = self.root.after(2000, self._on_filter_debounce_fire)
        else:
            self._save_current_filter()
            self.on_filter_changed()
    
    def _cancel_filter_debounce(self):
        """取消过滤防抖定时器"""
        if self._filter_debounce_job:
            self.root.after_cancel(self._filter_debounce_job)
            self._filter_debounce_job = None
    
    def _on_filter_debounce_fire(self):
        """防抖定时器触发，执行过滤"""
        self._filter_debounce_job = None
        self._save_current_filter()
        self.on_filter_changed()
    
    def on_filter_focus_out(self, event):
        """过滤框失去焦点时立即触发过滤"""
        self._cancel_filter_debounce()
        self._save_current_filter()
        self.on_filter_changed()
    
    def _save_current_filter(self):
        """保存当前输入的关键字到历史"""
        keyword = self.filter_entry.get().strip()
        if keyword and keyword not in self.filter_history:
            self.filter_history.insert(0, keyword)
            if len(self.filter_history) > 20:
                self.filter_history = self.filter_history[:20]
            self.filter_entry['values'] = self.filter_history

    # ========== 辅助与增强功能 ==========

    def _add_text_context_menu(self, widget):
        """为文本组件添加右键菜单和 Ctrl+A 全选快捷键"""
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label=tr("全选"), command=lambda: self._select_all(widget))
        menu.add_command(label=tr("复制"), command=lambda: self._copy_selection(widget))
        menu.add_separator()
        menu.add_command(label=tr("清空"), command=lambda: widget.delete("1.0", tk.END))

        def show_menu(event):
            menu.post(event.x_root, event.y_root)

        widget.bind("<Button-3>", show_menu)
        widget.bind("<Button-3>", show_menu)
        widget.bind("<Control-a>", lambda e: self._select_all(widget))
        widget.bind("<Control-A>", lambda e: self._select_all(widget))
        widget.bind("<Control-c>", lambda e: (self._copy_selection(widget), "break")[-1])
        widget.bind("<Control-C>", lambda e: (self._copy_selection(widget), "break")[-1])

    def _select_all(self, widget):
        widget.tag_add("sel", "1.0", "end")
        widget.mark_set("insert", "end")
        return "break" # 取消默认行为

    def _copy_selection(self, widget):
        try:
            # 优先从 widget 中直接获取选中的文本
            selected_text = widget.get(tk.SEL_FIRST, tk.SEL_LAST)
            if selected_text:
                self.root.clipboard_clear()
                self.root.clipboard_append(selected_text)
                self.root.update() # 确保剪贴板立即更新
        except tk.TclError:
            pass # 可能没有选择内容
        except Exception as e:
            print(f"copy to clipboard failed: {e}")

    def _on_filter_double_click(self, event):
        """双击过滤窗口行，同步跳转到主接收窗口对应行"""
        # 获取点击位置的整行文本
        index = self.filter_text.index(f"@{event.x},{event.y} linestart")
        line_content = self.filter_text.get(index, f"{index} lineend").strip()
        if not line_content:
            return
            
        # 提取时间戳作为搜索锚点，格式一般为 [HH:MM:SS.mmm]
        # 如果没有时间戳，则直接搜整个内容（可能不唯一）
        search_pattern = ""
        import re
        ts_match = re.search(r"\[(\d{2}:\d{2}:\d{2}\.\d{3})\]", line_content)
        if ts_match:
            search_pattern = ts_match.group(0) # 包含中括号
        else:
            search_pattern = line_content[:30] # 取前30个字符
            
        if not search_pattern:
            return
            
        # 在主窗口中搜索
        target_idx = self.main_text.search(search_pattern, "1.0", stopindex=tk.END)
        if target_idx:
            # 跳转
            self.main_text.see(target_idx)
            # 视觉闪烁提示：临时高亮整行
            self.main_text.tag_add("jump_highlight", f"{target_idx} linestart", f"{target_idx} lineend")
            self.root.after(500, lambda: self.main_text.tag_remove("jump_highlight", "1.0", tk.END))
            # 切换到主数据 Tab (虽然现在是 PanedWindow 上下结构，但可能需要 lift 等)
            # 目前是上下分栏，直接 see 即可
    
if __name__ == "__main__":
    root = tk.Tk()
    
    # 设置窗口图标
    try:
        icon = tk.PhotoImage(file=get_icon_path(32))
        root.iconphoto(False, icon)
    except Exception as e:
        print(f"failed to load the window icon: {e}")
    
    app = SerialReceiver(root)
    root.mainloop()
