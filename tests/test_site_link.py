"""Checks for the company website link under the window name.

Run:  python tests/test_site_link.py
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
home = tempfile.mkdtemp()
os.environ["USERPROFILE"] = home
os.environ["HOME"] = home
os.environ["XTELL_SERIAL_HUB_PORT"] = "50975"
sys.path.insert(0, HERE)
os.chdir(HERE)

import tkinter as tk  # noqa: E402
import xtell_serial as X  # noqa: E402

opened = []
X.webbrowser.open = lambda url, *a, **k: opened.append(url) or True

root = tk.Tk()
root.geometry("1000x700")
app = X.SerialReceiver(root)
root.update()
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail and not ok else ""))


print("website link")
check("the link shows the company address", app.site_link.cget("text") == "www.xtell.cn")
check("it uses a hand cursor", str(app.site_link.cget("cursor")) == "hand2")
check("it sits under the window name", app.site_link.winfo_y() > app.custom_name_label.winfo_y())
check("it is not cut off at the default window size", app.site_link.winfo_rootx() + app.site_link.winfo_width() <= root.winfo_rootx() + root.winfo_width())

app.site_link.event_generate("<Button-1>")
root.update()
check("a click opens the website once", len(opened) == 1, opened)
check("the address carries the source tag",
      opened and opened[0] == "https://www.xtell.cn/?utm_source=xtell-serial&utm_medium=app&utm_content=window", opened)

tip_zh = app.site_link._tooltip.text
app._toggle_language()
tip_other = app.site_link._tooltip.text
app._toggle_language()
check("the hover text follows the interface language", tip_zh != tip_other and tip_zh == app.site_link._tooltip.text, (tip_zh, tip_other))

# the name label keeps working with the link below it
app.custom_name_var.set("gateway")
root.update()
check("a window name and the link can show together", app.site_link.winfo_y() > app.custom_name_label.winfo_y() + app.custom_name_label.winfo_height() - 2)

print(f"\n{sum(results)}/{len(results)} passed")
sys.stdout.flush()
os._exit(0 if all(results) else 1)
