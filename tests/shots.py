"""Screenshots of the real window for visual checks (not a pytest test).

    python tests/shots.py <photo folder> <out dir> [lang ...] [--min] [--dark] [--long]

Opens the folder, waits for the plan, grabs the window (default size and, with
--min, the minimum size) for each language. Settings go to <out dir>, never
next to the program. Look at every image: a passing test suite once showed an
empty table, and a capture once grabbed the lock screen.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def screen_locked() -> bool:
    """While Windows is locked, the window under any point is the lock screen."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    hwnd = ctypes.windll.user32.WindowFromPoint(wintypes.POINT(100, 100))
    buf = ctypes.create_unicode_buffer(64)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 64)
    return buf.value == "LockScreenBackstopFrame"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    folder, out = os.path.abspath(args[0]), os.path.abspath(args[1])
    langs = args[2:] or ["ko"]
    os.makedirs(out, exist_ok=True)
    import i18n

    i18n._settings_path = os.path.join(out, "settings.json")
    if "--dark" in flags:
        i18n.update_settings(theme="dark")
    import gui
    import theme
    from PIL import ImageGrab

    gui._enable_dpi_awareness()
    i18n.init(langs[0])
    root = gui.TkinterDnD.Tk() if gui._HAS_DND else gui.tk.Tk()
    gui.size_window(root)  # the same scaling as the real program
    px = theme.px
    sizes = [(px(theme.WINDOW[0]), px(theme.WINDOW[1]))]
    if "--min" in flags:
        sizes.append((px(theme.WINDOW_MIN[0]), px(theme.WINDOW_MIN[1])))
    root.geometry(f"{sizes[0][0]}x{sizes[0][1]}+20+20")
    app = gui.App(root, None if "--empty" in flags else folder)
    jobs = [(lang, size) for lang in langs for size in sizes]
    state = {"n": 0, "waited": 0}

    def grab(name: str) -> None:
        root.update()
        if screen_locked():
            print("LOCKED: the PC is locked, a capture would show the lock screen (LESSONS D1)", flush=True)
            root.destroy()
            sys.exit(3)
        x, y = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(os.path.join(out, name))
        print("saved", name, w, h, flush=True)

    def step() -> None:
        if "--debug" in flags:
            print("step", state, app.busy, app.plan is not None, app.replan_job, flush=True)
        ready = "--empty" in flags or (app.plan is not None and not app.busy and app.replan_job is None)
        if not ready:
            state["waited"] += 1
            if state["waited"] > 600:
                print("timeout", flush=True)
                root.destroy()
                return
            root.after(100, step)
            return
        if state["n"] >= len(jobs):
            root.destroy()
            return
        lang, size = jobs[state["n"]]
        state["n"] += 1
        if i18n.current_lang() != lang:
            i18n.set_lang(lang, persist=False)
            app._build()
            app.request_plan(now=True)
        root.geometry(f"{size[0]}x{size[1]}")
        root.after(800, lambda: (grab(f"{lang}_{size[0]}x{size[1]}{'_dark' if '--dark' in flags else ''}.png"),
                                 root.after(200, step)))

    root.after(500, step)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
