"""Correctness checks for the receive pipeline (no serial hardware, no main loop).

Run:  python tests/test_pipeline.py
"""

import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
home = tempfile.mkdtemp()
os.environ["USERPROFILE"] = home
os.environ["HOME"] = home
os.environ["XTELL_SERIAL_HUB_PORT"] = "50972"
sys.path.insert(0, HERE)
os.chdir(HERE)

import tkinter as tk  # noqa: E402
import xtell_serial as X  # noqa: E402

root = tk.Tk()
root.withdraw()
app = X.SerialReceiver(root)
root.update()
app.running = True

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail and not ok else ""))


def feed(data):
    entries = []
    app._parse_rx_bytes(data, entries)
    if entries:
        app._commit_entries(entries)
    return entries


def reset():
    app._clear_main()
    app.encoding = "utf-8"
    app.max_lines = X.DEFAULT_MAX_LINES
    app.filter_entry.delete(0, tk.END)
    app.on_filter_changed()
    app.show_hex_var.set(False)
    app.show_line_number_var.set(True)
    app.show_timestamp_var.set(True)


def main_text():
    return app.main_text.get("1.0", "end-1c")


print("receive pipeline")

reset()
feed(b"hello\r\nworld\r\n")
check("two complete lines become two entries", [e[0] for e in app.all_lines] == ["hello\r\n", "world\r\n"])
check("line numbers count up from 1", [e[2] for e in app.all_lines] == [1, 2])
check("hex of each line is kept", app.all_lines[0][3] == b"hello\r\n".hex())
check("both lines are drawn", "hello" in main_text() and "world" in main_text())

reset()
data = "你好，世界\n".encode("utf-8")
feed(data[:4])
feed(data[4:])
check("a multi-byte character split across reads is decoded intact", app.all_lines and app.all_lines[0][0] == "你好，世界\n",
      repr(app.all_lines))

reset()
app.encoding = "gbk"
feed("温度=25\n".encode("gbk"))
check("GBK text is decoded", app.all_lines and app.all_lines[0][0] == "温度=25\n")

reset()
feed(b"partial")
check("an unfinished line is held back", len(app.all_lines) == 0)
app.last_data_time = time.time() - 1
app._timeout_flush()
check("the unfinished line is shown after 100 ms of silence", [e[0] for e in app.all_lines] == ["partial"])

reset()
app.filter_entry.insert(0, "ERR")
app.on_filter_changed()
feed(b"INF a\nERR b\nINF c\nERR d\n")
filt = app.filter_text.get("1.0", "end-1c")
check("filter pane shows only matching lines", "ERR b" in filt and "ERR d" in filt and "INF" not in filt, filt)
check("filter count is right", app.filter_count_label.cget("text") == "2")
check("matches are highlighted in the main view", len(app.main_text.tag_ranges("highlight")) == 4)

reset()
app.filter_entry.insert(0, r"\d+")
app.filter_mode_var.set("regex")
app.on_filter_changed()
feed(b"a1 b22\nnone\n")
check("regex filter matches", app.filter_count_label.cget("text") == "1")
app.filter_mode_var.set("string")

reset()
app.filter_entry.insert(0, "(")
app.filter_mode_var.set("regex")
app.on_filter_changed()
feed(b"x(\n")
check("a bad regex does not crash and shows an error", app.regex_error_label.cget("text") != "")
app.filter_mode_var.set("string")

reset()
big = b"".join(b"line %d\n" % i for i in range(5000))
feed(big)
check("a burst keeps every line in the buffer", len(app.all_lines) == 5000)
check("a burst draws the newest lines, not the oldest", "line 4999" in main_text() and "line 10\n" not in main_text())
check("the notice mentions the skipped count", "4400" in main_text().split("\n")[0], main_text().split("\n")[0][:80])

reset()
app.max_lines = 200
for i in range(3):
    feed(b"".join(b"row %d\n" % (i * 500 + k) for k in range(500)))
check("the buffer is trimmed once it passes the limit plus slack", len(app.all_lines) <= 200 + X.SerialReceiver.TRIM_SLACK,
      len(app.all_lines))
check("the newest line survives trimming", app.all_lines[-1][0] == "row 1499\n")
check("the screen is trimmed too", int(app.main_text.index("end-1c").split(".")[0]) <= 200 + X.SerialReceiver.TRIM_SLACK + 5)

reset()
feed(b"abc\n")
app.show_hex_var.set(True)
app.refresh_all_display()
check("HEX view redraws the lines as hex", b"abc\n".hex() in main_text())
app.show_hex_var.set(False)
app.refresh_all_display()
check("switching HEX off redraws as text", "abc" in main_text() and b"abc\n".hex() not in main_text())

reset()
path = os.path.join(home, "save.log")
app.save_file_handle = open(path, "a", encoding="utf-8")
app.show_timestamp_var.set(False)
feed(b"one\ntwo\n")
app.save_file_handle.close()
app.save_file_handle = None
check("lines are written to the save file", open(path, encoding="utf-8").read() == "one\ntwo\n")

reset()
feed(b"x\n")
app._clear_main()
check("clearing empties the buffer and the pending data", len(app.all_lines) == 0 and app._rx_partial == b"")

passed = sum(results)
print(f"\n{passed}/{len(results)} passed")
sys.stdout.flush()
os._exit(0 if passed == len(results) else 1)
