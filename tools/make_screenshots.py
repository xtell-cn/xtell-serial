"""Make the documentation screenshots from the real GUI, with synthetic demo data.

It starts the actual xtell-serial window, feeds it a made-up boot log (no hardware,
no real device), and saves screenshots of the window to the output folder.
The log content is demo data; say so in the caption wherever the images are used.

Usage:
    python tools/make_screenshots.py --lang en --out docs/images
    python tools/make_screenshots.py --lang zh --out docs/images

The window comes to the front for a few seconds while it is captured.
"""

import argparse
import ctypes
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Physical pixels, so the captured window matches the coordinates Tk reports.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

DEMO_BOOT = [
    "[boot] reset cause: power-on",
    "[boot] firmware 1.4.2 (build 2026-10-12)",
    "[boot] flash 4096 KB, psram 8192 KB",
    "[net] wifi: connecting to \"lab-ap\" ...",
    "[net] wifi: connected, ip 192.168.1.42, rssi -58 dBm",
    "[mqtt] connecting to broker ...",
    "[mqtt] connected, subscribed to device/cmd",
    "[sensor] temp=24.6 C hum=48 %",
    "[sensor] temp=24.7 C hum=48 %",
    "[WARN] battery 3.52 V (low threshold 3.50 V)",
    "[sensor] temp=24.7 C hum=47 %",
    "[ERR] i2c: timeout addr=0x44",
    "[sensor] retry 1/3",
    "[ERR] i2c: timeout addr=0x44",
    "[sensor] retry 2/3",
    "[sensor] temp=24.8 C hum=47 %",
    "[mqtt] publish telemetry ok (212 bytes)",
    "[sensor] temp=24.8 C hum=47 %",
    "[WARN] battery 3.49 V (below threshold)",
    "[ERR] power: brownout detected, restarting radio",
    "[net] wifi: disconnected (reason 200)",
    "[net] wifi: connecting to \"lab-ap\" ...",
    "[net] wifi: connected, ip 192.168.1.42, rssi -61 dBm",
    "[mqtt] reconnecting ...",
    "[mqtt] connected, subscribed to device/cmd",
    "[sensor] temp=24.9 C hum=46 %",
]


def frame_rect(root):
    """Window rectangle including the title bar, without the invisible resize border."""
    from ctypes import wintypes
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    rect = wintypes.RECT()
    ok = ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect))
    if ok != 0:
        ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def pump(root, seconds):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.01)


def grab(root, path):
    from PIL import ImageGrab
    root.lift()
    root.attributes("-topmost", True)
    pump(root, 0.6)
    img = ImageGrab.grab(bbox=frame_rect(root), all_screens=True)
    img.save(path)
    print("saved", path, img.size)


def name_window(app, root, name):
    from i18n import tr
    app.custom_name_var.set(name)
    app.instance_name = name
    root.title(f"{tr('极智调试串口')} - {name}")
    app.custom_name_label.config(font=("Microsoft YaHei", 20, "bold"), foreground="green")


def close_about(*roots):
    """The app opens its About box 0.5 s after start; wait for it and close it."""
    import tkinter as tk
    for r in roots:
        pump(r, 0.9)
        for child in r.winfo_children():
            if isinstance(child, tk.Toplevel) and child.title() in ("About", "关于"):
                child.destroy()
        pump(r, 0.2)


def feed_lines(app, root, lines, pause=0.12):
    for line in lines:
        entries = []
        app._parse_rx_bytes((line + "\r\n").encode("utf-8"), entries)
        app._commit_entries(entries)
        pump(root, pause)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=("en", "zh"), default="en")
    ap.add_argument("--out", default=os.path.join(HERE, "docs", "images"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    # A private config folder, so the user's saved language setting is not touched.
    home = tempfile.mkdtemp()
    os.makedirs(os.path.join(home, ".xtell-serial"))
    with open(os.path.join(home, ".xtell-serial", "config.json"), "w", encoding="utf-8") as f:
        json.dump({"lang": args.lang}, f)
    os.environ["USERPROFILE"] = home
    os.environ["HOME"] = home
    os.environ["XTELL_SERIAL_HUB_PORT"] = "50974"
    sys.path.insert(0, HERE)
    os.chdir(HERE)

    import tkinter as tk
    import xtell_serial as X
    from i18n import tr

    root = tk.Tk()
    app = X.SerialReceiver(root)
    root.geometry("1180x760+60+40")  # after the app, which sets its own default size
    root.update()
    close_about(root)

    app.running = True
    app.port_combo.set("COM3")
    app.baud_combo.set("115200")
    app.open_btn.config(text=tr("关闭连接"))
    app._set_status("状态: 已连接 {port} {baud}bps", "green", port="COM3", baud=115200)
    name_window(app, root, "gateway")
    suffix = args.lang

    # 1. Main window: the log, with the filter highlighting errors
    app.filter_entry.delete(0, tk.END)
    app.filter_entry.insert(0, "ERR")
    app.on_filter_changed()
    feed_lines(app, root, DEMO_BOOT)
    pump(root, 0.5)
    grab(root, os.path.join(args.out, f"serial-main-window.{suffix}.png"))

    # 2. A burst of data: the window stays responsive and says what it did not draw
    app._clear_main()
    app.max_lines = 20000
    app.filter_entry.delete(0, tk.END)
    app.filter_entry.insert(0, "ERR")
    app.on_filter_changed()
    burst = []
    for i in range(18500):
        level = "ERR" if i % 700 == 0 else "INF"
        burst.append(f"[{i:06d}] {level} adc ch{i % 8}={(i * 37) % 4096:04d}")
    entries = []
    app._parse_rx_bytes(("\r\n".join(burst) + "\r\n").encode("utf-8"), entries)
    app._commit_entries(entries)
    pump(root, 0.6)
    # show the notice line at the top of the view
    app.paused_main = True
    pos = app.main_text.search("Data burst", "1.0", stopindex=tk.END) or app.main_text.search("数据突发", "1.0", stopindex=tk.END)
    if pos:
        app.main_text.yview(pos.split(".")[0] + ".0")
    pump(root, 0.4)
    grab(root, os.path.join(args.out, f"serial-burst.{suffix}.png"))

    # 3. Two named windows side by side
    app.paused_main = False
    app._clear_main()
    app.max_lines = X.DEFAULT_MAX_LINES
    feed_lines(app, root, DEMO_BOOT[:14], pause=0.03)
    app.filter_entry.delete(0, tk.END)
    app.on_filter_changed()
    root.geometry("940x640+20+60")

    second_root = tk.Toplevel(root)
    second = X.SerialReceiver(second_root)
    second_root.geometry("940x640+965+60")
    close_about(root, second_root)
    second.running = True
    second.port_combo.set("COM7")
    second.baud_combo.set("9600")
    second.open_btn.config(text=tr("关闭连接"))
    second._set_status("状态: 已连接 {port} {baud}bps", "green", port="COM7", baud=9600)
    name_window(second, second_root, "sensor")
    feed_lines(second, second_root, [
        "AT+GMR", "AT version:2.2.0.0", "SDK version:v4.3", "OK",
        "AT+CWMODE?", "+CWMODE:1", "OK", "AT+CIFSR", "+CIFSR:STAIP,\"192.168.1.57\"", "OK",
    ], pause=0.03)
    pump(root, 0.8)

    from PIL import ImageGrab
    for w in (root, second_root):
        w.lift()
    pump(root, 0.6)
    left = frame_rect(root)
    right = frame_rect(second_root)
    box = (min(left[0], right[0]), min(left[1], right[1]), max(left[2], right[2]), max(left[3], right[3]))
    img = ImageGrab.grab(bbox=box, all_screens=True)
    path = os.path.join(args.out, f"serial-two-windows.{suffix}.png")
    img.save(path)
    print("saved", path, img.size)

    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
