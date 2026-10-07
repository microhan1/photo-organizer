"""tkinter GUI with the Sun Valley theme (sv-ttk).

One window, top to bottom: header (name, folder, language, settings), the drop
area (a summary strip after the scan), one row of options, the preview table,
the bottom bar (summary numbers, progress, Undo, Run).

Long work (scan, hashing, similar photos, running, undo) runs on a worker
thread; results come back through a queue polled with after(). Workers never
touch widgets or tk variables: everything they need is read before they start.
"""
from __future__ import annotations

import collections
import dataclasses
import datetime
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont

import dedupe
import i18n
import main as core
import mover
import plan as plan_mod
import prefs as prefs_mod
import scan as scan_mod
import theme
import undo as undo_mod
from longpath import fs
from i18n import t

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _HAS_DND = True
except Exception:  # pragma: no cover - optional dependency
    _HAS_DND = False

px = theme.px  # design pixels -> screen pixels (DPI scaling)
GEAR = chr(0x2699)
ELLIPSIS = chr(0x2026)
DOT = chr(0x00B7)
PATTERN_CHARS = 20  # folder pattern box width (characters)
DEST_CHARS = 22  # destination button width (characters)
POLL_MS = 50
REPLAN_DELAY_MS = 250
THUMB_CACHE = 500
THUMB_WARN_COUNT = 1000
COLUMNS = (("current", 300, True), ("date", 128, False), ("basis", 120, False), ("new", 300, True),
           ("status", 150, False))
VIEWS = ("all", "todo", "nodate", "dup", "failed")
DATE_INPUTS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y.%m.%d", "%Y%m%d")


def _enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def rel(path: str, base: str) -> str:
    """Path relative to base for display, with "/" so it reads the same in every font
    (Korean and Japanese fonts draw a backslash as a currency sign)."""
    try:
        r = os.path.relpath(path, base)
    except ValueError:
        r = path
    if r.startswith(".."):
        r = path
    return r.replace(os.sep, "/")


def short(path: str, limit: int = 60) -> str:
    """Middle ellipsis: keeps the drive and the last folders, which are what people recognise."""
    if len(path) <= limit:
        return path
    keep = limit - 1
    return path[: keep // 3] + ELLIPSIS + path[-(keep - keep // 3):]


def parse_date(text: str) -> datetime.datetime | None:
    s = text.strip()
    for fmt in DATE_INPUTS:
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def open_path(path: str, select: bool = False) -> None:
    try:
        if sys.platform == "win32":
            if select:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path if not select else os.path.dirname(path)])
    except OSError:
        pass


def fmt_count(n: int) -> str:
    return f"{n:,}"


# ------------------------------------------------------------------ virtual table
class VirtualTable(ttk.Frame):
    """A Treeview that only ever holds the rows on screen, so 100,000 lines cost
    nothing. ``fetch(index)`` gives (values, tags, image) for one row of the view."""

    def __init__(self, master, fetch, on_select=None) -> None:
        super().__init__(master)
        self.fetch = fetch
        self.on_select = on_select
        self.count = 0
        self.top = 0
        self.visible = 1
        self.selected: int | None = None
        self.row_height = px(theme.ROW_HEIGHT)
        self._header = 0
        self._quiet = False
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self._scroll)
        self.vsb.pack(side="right", fill="y")
        self.tree = ttk.Treeview(self, columns=[c[0] for c in COLUMNS], show="headings", selectmode="browse")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Configure>", lambda e: self.after_idle(self.layout))
        self.tree.bind("<MouseWheel>", self._wheel)
        self.tree.bind("<<TreeviewSelect>>", self._picked)
        for key, step in (("<Up>", -1), ("<Down>", 1), ("<Prior>", "-page"), ("<Next>", "page"),
                          ("<Home>", "home"), ("<End>", "end")):
            self.tree.bind(key, lambda e, s=step: self._key(s))

    def set_thumbs(self, on: bool) -> None:
        self.row_height = px(theme.ROW_HEIGHT_THUMB) if on else px(theme.ROW_HEIGHT)
        self.tree.configure(show="tree headings" if on else "headings")
        self.tree.column("#0", width=px(theme.THUMB + 16) if on else 0, stretch=False, minwidth=0)
        self._header = 0
        self.layout()

    def set_count(self, n: int) -> None:
        self.count = n
        if self.selected is not None and self.selected >= n:
            self.selected = None
        self.top = max(0, min(self.top, n - self.visible))
        self.refresh()

    def layout(self) -> None:
        h = self.tree.winfo_height()
        header = self._header or px(28)
        self.visible = max(1, (h - header) // self.row_height)
        self.top = max(0, min(self.top, self.count - self.visible))
        self.refresh()

    def refresh(self) -> None:
        """Redraw the rows on screen; keeps the selection and the scroll position itself."""
        tree = self.tree
        tree.delete(*tree.get_children())
        end = min(self.count, self.top + self.visible)
        for idx in range(self.top, end):
            values, tags, image = self.fetch(idx)
            kw = {"image": image} if image is not None else {}
            tree.insert("", "end", iid=str(idx), values=values, tags=tags, **kw)
        if self.count and not self._header:
            box = tree.bbox(str(self.top))
            if box:
                self._header = box[1]
                self.visible = max(1, (tree.winfo_height() - self._header) // self.row_height)
                if self.top + self.visible > end and end < self.count:
                    self.after_idle(self.refresh)
        if self.selected is not None and self.top <= self.selected < end:
            self._quiet = True
            tree.selection_set(str(self.selected))
            tree.focus(str(self.selected))
            self._quiet = False
        if self.count:
            self.vsb.set(self.top / self.count, min(1.0, (self.top + self.visible) / self.count))
        else:
            self.vsb.set(0, 1)

    def see(self, idx: int) -> None:
        if idx < self.top:
            self.top = idx
        elif idx >= self.top + self.visible:
            self.top = idx - self.visible + 1
        self.refresh()

    def visible_range(self) -> range:
        return range(self.top, min(self.count, self.top + self.visible))

    def _scroll(self, *args) -> None:
        if args[0] == "moveto":
            self.top = int(float(args[1]) * self.count)
        elif args[0] == "scroll":
            n = int(args[1])
            self.top += n * (self.visible if args[2] == "pages" else 1)
        self.top = max(0, min(self.top, self.count - self.visible))
        self.refresh()

    def _wheel(self, event) -> str:
        self.top = max(0, min(self.top - int(event.delta / 120) * 3, self.count - self.visible))
        self.refresh()
        return "break"

    def _key(self, step) -> str:
        if not self.count:
            return "break"
        cur = self.selected if self.selected is not None else self.top
        if step == "page":
            cur += self.visible
        elif step == "-page":
            cur -= self.visible
        elif step == "home":
            cur = 0
        elif step == "end":
            cur = self.count - 1
        else:
            cur += step
        self.select(max(0, min(cur, self.count - 1)))
        return "break"

    def select(self, idx: int) -> None:
        self.selected = idx
        self.see(idx)
        if self.on_select:
            self.on_select(idx)

    def _picked(self, _event) -> None:
        if self._quiet:
            return
        sel = self.tree.selection()
        if sel:
            self.selected = int(sel[0])
            if self.on_select:
                self.on_select(self.selected)

    def index_at(self, y: int) -> int | None:
        iid = self.tree.identify_row(y)
        return int(iid) if iid else None


# ------------------------------------------------------------------ thumbnails
class Thumbs:
    """48x48 thumbnails for the rows on screen, made on one background thread
    (PIL images); PhotoImages are created on the Tk thread. At most THUMB_CACHE kept."""

    def __init__(self, post) -> None:
        self.post = post
        self.cache: collections.OrderedDict[str, object] = collections.OrderedDict()
        self.wanted: queue.Queue[str] = queue.Queue()
        self.asked: set[str] = set()
        threading.Thread(target=self._loop, daemon=True).start()

    def get(self, path: str):
        img = self.cache.get(path)
        if img is not None:
            self.cache.move_to_end(path)
            return img
        if path not in self.asked:
            self.asked.add(path)
            self.wanted.put(path)
        return None

    def put(self, path: str, pil) -> None:
        from PIL import ImageTk

        self.asked.discard(path)
        if pil is None:
            return
        self.cache[path] = ImageTk.PhotoImage(pil)
        while len(self.cache) > THUMB_CACHE:
            self.cache.popitem(last=False)

    def clear(self) -> None:
        self.cache.clear()
        self.asked.clear()

    def _loop(self) -> None:
        from PIL import Image, ImageOps

        while True:
            path = self.wanted.get()
            pil = None
            try:
                if os.path.splitext(path)[1].lower() in scan_mod.HEIC_EXTS | scan_mod.AVIF_EXTS:
                    import pillow_heif

                    pillow_heif.register_heif_opener()
                with Image.open(fs(path)) as im:
                    im.draft("RGB", (px(theme.THUMB) * 2, px(theme.THUMB) * 2))
                    im = ImageOps.exif_transpose(im)
                    im.thumbnail((px(theme.THUMB), px(theme.THUMB)))
                    pil = im.convert("RGB")
            except Exception:
                pil = None
            self.post("thumb", (path, pil))


# ------------------------------------------------------------------ app
@dataclasses.dataclass
class Ui:
    """The settings a worker needs, read on the Tk thread before it starts."""
    source: str
    dest: str
    mode: str
    heic_only: bool
    convert: bool
    dedupe: bool
    similar: bool


class App:
    def __init__(self, root: tk.Tk, folder: str | None = None) -> None:
        self.root = root
        self.prefs = prefs_mod.load()
        self.cache = scan_mod.MetaCache()
        self.queue: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = ""
        self.source = ""
        self.dest = self.prefs.dest
        self.scan_result: scan_mod.ScanResult | None = None
        self.plan: plan_mod.Plan | None = None
        self.serial = 0  # settings generation; bumps on every change
        self.plan_serial = -1  # generation the shown plan was built for
        self.replan_job = None
        self.similar_map: dict | None = None
        self.overrides = plan_mod.Overrides()
        self.view: list[int] = []
        self.view_name = "all"
        self.fail_reasons: dict[str, str] = {}
        self.last_result: mover.Result | None = None
        self.band_kind = ""
        self.thumbs = Thumbs(self._post)
        self.thumb_refresh = None
        self.heic_only = tk.BooleanVar(value=False)
        self._apply_theme()
        self.content: ttk.Frame | None = None
        self._build()
        root.after(POLL_MS, self._poll)
        if _HAS_DND:
            try:
                root.drop_target_register(DND_FILES)
                root.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass
        if folder and os.path.isdir(folder):
            root.after(100, lambda: self.open_folder(folder))

    # -------------------------------------------------------------- look
    def _apply_theme(self) -> None:
        try:
            import sv_ttk

            sv_ttk.set_theme("dark" if theme.is_dark(self.prefs.theme) else "light")
        except Exception:
            pass
        self.colors = theme.palette(self.prefs.theme)
        self.root.configure(bg=self.colors["bg"])

    def _fonts(self) -> None:
        families = set(tkfont.families(self.root))
        ui = theme.pick_font(families, theme.UI_FONTS.get(i18n.current_lang(), ()))
        table = theme.pick_font(families, theme.TABLE_FONTS)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(family=ui, size=theme.BODY_PT)
            except tk.TclError:
                pass
        self.f_body = (ui, theme.BODY_PT)
        self.f_bold = (ui, theme.BODY_PT, "bold")
        self.f_num = (ui, theme.NUMBER_PT, "bold")
        self.f_drop = (ui, theme.BODY_PT + 4, "bold")
        self.f_table = (table, theme.BODY_PT)
        style = ttk.Style(self.root)
        style.configure("Treeview", font=self.f_table, rowheight=px(theme.ROW_HEIGHT))
        style.configure("Treeview.Heading", font=self.f_body)
        style.map("Treeview", background=[("selected", self.colors["accent"])], foreground=[("selected", "#FFFFFF")])
        for w in ("TLabel", "TButton", "TCheckbutton", "TRadiobutton", "TCombobox", "TEntry", "TNotebook.Tab"):
            style.configure(w, font=self.f_body)
        style.configure("Muted.TLabel", foreground=self.colors["muted"])
        style.configure("Warn.TLabel", foreground=self.colors["warn"])
        style.configure("Num.TLabel", font=self.f_num)
        style.configure("NumMuted.TLabel", font=self.f_num, foreground=self.colors["muted"])
        style.configure("Name.TLabel", font=self.f_bold)
        self.root.option_add("*TCombobox*Listbox.font", self.f_body)  # the drop-down list of every combobox

    # -------------------------------------------------------------- build
    def _build(self) -> None:
        """(Re)create every widget; called again when the language changes."""
        if self.content is not None:
            self.content.destroy()
        self._fonts()
        self.root.title(t("app_title"))
        P, PO = px(theme.PAD), px(theme.PAD_OUT)
        self.content = ttk.Frame(self.root, padding=(PO, PO - P, PO, PO))
        self.content.pack(fill="both", expand=True)
        c = self.content
        # bottom first, so the Run button can never be pushed out of the window (organizer A15)
        self.footer = ttk.Frame(c)
        self.footer.pack(side="bottom", fill="x", pady=(P, 0))
        self.band_area = ttk.Frame(c)
        self.band_area.pack(side="bottom", fill="x")
        self._build_header(c)
        self.top_area = ttk.Frame(c)
        self.top_area.pack(side="top", fill="x", pady=(P, 0))
        self._build_options(c)
        self.table = VirtualTable(c, self._row, self._on_select)
        self.table.pack(side="top", fill="both", expand=True, pady=(P, 0))
        for cid, width, stretch in COLUMNS:
            self.table.tree.heading(cid, text=t(f"col_{cid}"), anchor="w")
            self.table.tree.column(cid, width=px(width), stretch=stretch, minwidth=px(60))
        self.table.tree.heading("#0", text=t("col_thumb"))
        self.table.set_thumbs(self.prefs.thumbnails)
        # a Treeview colours whole rows, not cells: "OK" rows keep the normal text colour (a table of
        # green rows is hard to read); the other statuses stand out. The status is always written too.
        for status, color in theme.STATUS_COLORS.items():
            self.table.tree.tag_configure(status, foreground=self.colors["text"] if status == "ok" else color)
        self.table.tree.tag_configure("off", foreground=self.colors["muted"])
        self.table.tree.bind("<Double-1>", self._on_double)
        self.table.tree.bind("<Button-3>", self._on_menu)
        self._build_footer()
        self._show_top()
        self._refresh_view()
        self._update_buttons()
        if self.band_kind:
            self._show_band(self.band_kind)

    def _build_header(self, c) -> None:
        P = px(theme.PAD)
        bar = ttk.Frame(c)
        bar.pack(side="top", fill="x")
        # fixed-size widgets on the right first; the path label stretches into what is left
        ttk.Button(bar, text=GEAR, width=3, command=self.open_settings).pack(side="right")
        names = [i18n.LANG_NAMES[code] for code in i18n.LANGS]
        self.lang_box = ttk.Combobox(bar, values=names, state="readonly", width=10, font=self.f_body)
        self.lang_box.set(i18n.LANG_NAMES[i18n.current_lang()])
        self.lang_box.bind("<<ComboboxSelected>>", self._on_lang)
        self.lang_box.pack(side="right", padx=(0, P))
        ttk.Label(bar, text=t("app_title"), style="Name.TLabel").pack(side="left")
        self.path_label = ttk.Label(bar, text=short(self.source, 80), style="Muted.TLabel")
        self.path_label.pack(side="left", fill="x", expand=True, padx=(P * 2, P))

    def _build_options(self, c) -> None:
        P = px(theme.PAD)
        row = ttk.Frame(c)
        row.pack(side="top", fill="x", pady=(P, 0))
        self.options_row = row
        # one line must hold everything (PRD): the destination is a single button, and
        # "convert only" is the last choice of the folder pattern list rather than a toggle
        self.dest_btn = ttk.Button(row, text="", command=self.choose_dest, width=DEST_CHARS)
        self.dest_btn.pack(side="right")
        self.dest_btn.bind("<Button-3>", lambda e: self.reset_dest())
        self.pattern_label = ttk.Label(row, text=t("opt_pattern"))
        self.pattern_label.pack(side="left", padx=(0, P))
        self.pattern_box = ttk.Combobox(row, values=[*plan_mod.PRESETS, t("mode_heic_only")], width=PATTERN_CHARS, font=self.f_body)
        self.pattern_box.set(t("mode_heic_only") if self.heic_only.get() else self.prefs.pattern)
        self.pattern_box.bind("<<ComboboxSelected>>", lambda e: self._pattern_changed())
        self.pattern_box.bind("<KeyRelease>", lambda e: self._pattern_changed())
        self.pattern_box.pack(side="left", padx=(0, P * 2))
        self.v_convert = tk.BooleanVar(value=self.prefs.convert)
        self.v_dedupe = tk.BooleanVar(value=self.prefs.dedupe)
        self.v_similar = tk.BooleanVar(value=self.prefs.similar and self.similar_map is not None)
        self.cb_convert = ttk.Checkbutton(row, text=t("opt_convert"), variable=self.v_convert,
                                          command=lambda: self._toggle("convert", self.v_convert))
        self.cb_convert.pack(side="left", padx=(0, P * 2))
        self.cb_dedupe = ttk.Checkbutton(row, text=t("opt_dedupe"), variable=self.v_dedupe,
                                         command=lambda: self._toggle("dedupe", self.v_dedupe))
        self.cb_dedupe.pack(side="left", padx=(0, P * 2))
        self.cb_similar = ttk.Checkbutton(row, text=t("opt_similar"), variable=self.v_similar,
                                          command=self._similar_changed)
        self.cb_similar.pack(side="left")
        self.pattern_error = ttk.Label(c, text="", foreground=theme.ERR_TEXT)
        self._update_dest_button()
        self._mode_widgets()

    def _build_footer(self) -> None:
        P = px(theme.PAD)
        f = self.footer
        self.run_btn = ttk.Button(f, text=t("btn_run"), style="Accent.TButton", command=self.run_or_stop, width=10)
        self.run_btn.pack(side="right", anchor="se")  # always the bottom-right corner, even with a two-line summary
        self.undo_btn = ttk.Button(f, text=t("btn_undo"), command=self.undo, width=10)
        self.undo_btn.pack(side="right", anchor="se", padx=(0, P))
        self.summary_box = ttk.Frame(f)
        self.progress_box = ttk.Frame(f)
        self.progress = ttk.Progressbar(self.progress_box, mode="determinate", length=px(240))
        self.progress.pack(side="left")
        self.current_label = ttk.Label(self.progress_box, text="", style="Muted.TLabel")
        self.current_label.pack(side="left", fill="x", expand=True, padx=(P, P))
        self.v_mtime = tk.BooleanVar(value=self.prefs.use_mtime)
        self._fill_summary()
        self._show_footer_mode()
        f.bind("<Configure>", self._reflow_summary)

    def _fill_summary(self) -> None:
        for w in self.summary_box.winfo_children():
            w.destroy()
        s = self.plan.summary() if self.plan else {k: 0 for k in ("move", "convert", "nodate", "dupes", "same", "folders")}
        P = px(theme.PAD)
        self.sum_line = ttk.Frame(self.summary_box)
        self.sum_line.pack(side="top", anchor="w")
        self.sum_line2 = ttk.Frame(self.summary_box)  # holds the toggle when the line is too narrow
        self.mtime_cb = None
        for key in ("move", "convert", "nodate", "dupes", "same", "folders"):
            muted = key in ("nodate", "same")
            ttk.Label(self.sum_line, text=fmt_count(s[key]), style="NumMuted.TLabel" if muted else "Num.TLabel").pack(
                side="left")
            ttk.Label(self.sum_line, text=t(f"sum_{key}"), style="Muted.TLabel").pack(side="left", padx=(px(4), P + px(4)))
            if key == "nodate" and (s["nodate"] or self.prefs.use_mtime):
                # PRD: right next to the "no date" count; a child of summary_box so it can sit on either line
                self.mtime_cb = ttk.Checkbutton(self.summary_box, text=t("opt_use_mtime"), variable=self.v_mtime,
                                                command=self._mtime_changed)
                self.mtime_after = self.sum_line.winfo_children()[-1]
        self.root.after_idle(self._reflow_summary)  # once the labels have their sizes

    def _reflow_summary(self, _e=None) -> None:
        """The toggle goes right after the "no date" count when the bar is wide enough, else just below it.
        Decided from the numbers' own width (never including the toggle), so the choice cannot flip back
        and forth: an earlier version counted the toggle twice and froze the window resizing it forever."""
        if getattr(self, "mtime_cb", None) is None or not self.mtime_cb.winfo_exists():
            return
        P = px(theme.PAD)
        numbers = sum(w.winfo_reqwidth() + 2 * px(4) + P for w in self.sum_line.winfo_children())
        toggle = self.mtime_cb.winfo_reqwidth() + P + px(4)
        room = self.footer.winfo_width() - self.run_btn.winfo_reqwidth() - self.undo_btn.winfo_reqwidth() - 3 * P
        fits = numbers + toggle <= room
        here = self.mtime_cb.winfo_manager() and self.mtime_cb.pack_info().get("in")
        if fits and here != self.sum_line:
            self.sum_line2.pack_forget()
            self.mtime_cb.pack(in_=self.sum_line, side="left", after=self.mtime_after, padx=(0, P + px(4)))
        elif not fits and here != self.sum_line2:
            self.mtime_cb.pack(in_=self.sum_line2, side="left")
            self.sum_line2.pack(side="top", anchor="w")

    def _show_footer_mode(self) -> None:
        running = self.busy in ("run", "undo")
        self.summary_box.pack_forget()
        self.progress_box.pack_forget()
        if running:
            self.progress_box.pack(side="left", fill="x", expand=True)
        else:
            self.summary_box.pack(side="left", fill="x", expand=True)

    # -------------------------------------------------------------- top area
    def _clear_top(self) -> None:
        for w in self.top_area.winfo_children():
            w.destroy()

    def _show_top(self) -> None:
        if self.busy in ("scan", "similar"):
            self._show_scanning()
        elif self.scan_result is None:
            self._show_drop()
        else:
            self._show_strip()

    def _show_drop(self) -> None:
        self._clear_top()
        col = self.colors
        cv = tk.Canvas(self.top_area, height=px(300), bg=col["drop_bg"], highlightthickness=0, bd=0)
        cv.pack(fill="x")
        browse = ttk.Button(cv, text=t("btn_browse"), command=self.browse)

        def draw(_e=None) -> None:
            cv.delete("all")
            w, h = cv.winfo_width(), cv.winfo_height()
            cv.create_rectangle(2, 2, w - 3, h - 3, outline=col["muted"], dash=(6, 4), width=px(2))
            cx, cy = w // 2, h // 2 - px(56)
            # folder icon: tab and body
            pts = [(-32, -22), (-10, -22), (-4, -16), (32, -16), (32, 22), (-32, 22)]
            cv.create_polygon(*[c for x, y in pts for c in (cx + px(x), cy + px(y))], fill="", outline=col["accent"],
                              width=px(3), joinstyle="round")
            cv.create_text(cx, cy + px(50), text=t("drop_hint"), font=self.f_drop, fill=col["text"])
            cv.create_text(cx, cy + px(80), text=t("drop_sub"), font=self.f_body, fill=col["muted"],
                           width=max(px(200), w - px(64)))
            cv.create_window(cx, cy + px(122), window=browse)

        cv.bind("<Configure>", draw)

    def _show_scanning(self, text: str = "") -> None:
        self._clear_top()
        box = ttk.Frame(self.top_area, padding=(0, px(theme.PAD)))
        box.pack(fill="x")
        ttk.Button(box, text=t("btn_cancel"), command=self.cancel.set).pack(side="right")
        self.scan_bar = ttk.Progressbar(box, mode="determinate", length=px(320))
        self.scan_bar.pack(side="left")
        self.scan_label = ttk.Label(box, text=text or t("msg_reading_folder"))
        self.scan_label.pack(side="left", padx=px(theme.PAD))

    def _show_strip(self) -> None:
        self._clear_top()
        r = self.scan_result
        box = ttk.Frame(self.top_area)
        box.pack(fill="x")
        # fixed-size controls on the right first
        ttk.Button(box, text=t("btn_other_folder"), command=self.browse).pack(side="right")
        self.view_box = ttk.Combobox(box, state="readonly", width=16, font=self.f_body, values=[t(f"view_{v}") for v in VIEWS])
        self.view_box.set(t(f"view_{self.view_name}"))
        self.view_box.bind("<<ComboboxSelected>>", self._view_changed)
        self.view_box.pack(side="right", padx=(0, px(theme.PAD)))
        self.v_thumbs = tk.BooleanVar(value=self.prefs.thumbnails)
        ttk.Checkbutton(box, text=t("opt_thumbs"), variable=self.v_thumbs, command=self._thumbs_changed).pack(
            side="right", padx=(0, px(theme.PAD) * 2))
        if self.plan and any(i.suggest_dup for i in self.plan.items):
            ttk.Button(box, text=t("btn_apply_suggest"), command=self.apply_suggestions).pack(
                side="right", padx=(0, px(theme.PAD) * 2))
        text = t("scan_summary", folder=short(r.root, 48), count=fmt_count(len(r.media)), size=core.human_size(r.total_bytes))
        ttk.Label(box, text=text, style="Name.TLabel").pack(side="left")
        warns = []
        if r.cloud:
            warns.append(t("msg_cloud_warning", count=fmt_count(len(r.cloud))))
        if r.denied:
            warns.append(t("msg_denied", count=fmt_count(len(r.denied))))
        if scan_mod.network_path(r.root):
            warns.append(t("msg_network_slow"))
        if warns:
            ttk.Label(box, text=f"  {DOT} ".join(warns), style="Warn.TLabel").pack(side="left", padx=(px(theme.PAD) * 2, 0))

    # -------------------------------------------------------------- band
    def _clear_band(self) -> None:
        for w in self.band_area.winfo_children():
            w.destroy()

    def _show_band(self, kind: str, text: str = "") -> None:
        """kind: "done" (green), "failed" / "error" (red), "" (none)."""
        self.band_kind = kind
        self._clear_band()
        if not kind:
            return
        if text:
            self.band_text = text
        ok = kind == "done"
        bg = self.colors["ok_band"] if ok else self.colors["err_band"]
        fg = theme.OK_TEXT if ok else theme.ERR_TEXT
        band = tk.Frame(self.band_area, bg=bg, padx=px(theme.PAD) * 2, pady=px(theme.PAD))
        band.pack(fill="x", pady=(px(theme.PAD), 0))
        if kind in ("done", "failed"):
            ttk.Button(band, text=t("btn_undo"), command=self.undo).pack(side="right")
            ttk.Button(band, text=t("btn_open"), command=self.open_dest).pack(side="right", padx=(0, px(theme.PAD)))
        if kind == "failed" and self.fail_reasons:
            ttk.Button(band, text=t("btn_details"), command=self.show_failed).pack(side="right", padx=(0, px(theme.PAD)))
        tk.Label(band, text=getattr(self, "band_text", ""), bg=bg, fg=fg, font=self.f_bold, anchor="w",
                 justify="left", wraplength=px(640)).pack(side="left", fill="x", expand=True)

    # -------------------------------------------------------------- table
    def _row(self, idx: int):
        item = self.plan.items[self.view[idx]]
        m = item.primary
        current = rel(m.path, self.source)
        if len(item.members) > 1:
            current += "  " + " ".join("+" + x.ext.lstrip(".").upper() for x in item.members[1:])
        when = "-"
        if item.when is not None:
            when = item.when.strftime("%Y-%m-%d %H:%M") if item.when.time() != datetime.time(0) else \
                item.when.strftime("%Y-%m-%d")
        basis = t(f"basis_{item.basis}") if item.basis else "-"
        if not item.acts and item.status in (plan_mod.SAME, plan_mod.NODATE):
            new = ""
        elif item.status == plan_mod.DUP and not item.dst:
            new = t("lbl_trash")
        else:
            new = rel(item.dst, self.plan.options.dest)
        if item.convert_dst:
            new = (new + "  " if new and item.moves else "") + "+ " + os.path.basename(item.convert_dst)
        status = t(f"status_{item.status}")
        notes = [t(f"note_{n}") for n in item.notes if n != "pair_jpg"]
        if item.similar:
            notes.append(t("note_similar", n=item.similar))
            if item.suggest_dup and item.status != plan_mod.DUP:
                notes.append(t("note_suggest_dup"))
        if self.view_name == "failed" and item.key in self.fail_reasons:
            notes = [self.fail_reasons[item.key]]
        if notes:
            status += f" {DOT} " + f" {DOT} ".join(notes)
        tag = item.status if item.checked else "off"
        image = None
        if self.prefs.thumbnails:
            image = self.thumbs.get(m.path) or ""
        return (current, when, basis, new, status), (tag,), image

    def _refresh_view(self) -> None:
        """Recompute which items the table shows (filter) and redraw; selection kept by key."""
        keep = None
        if self.plan and self.table.selected is not None and self.table.selected < len(self.view):
            keep = self.plan.items[self.view[self.table.selected]].key
        if not self.plan:
            self.view = []
        else:
            items = self.plan.items
            name = self.view_name
            if name == "todo":
                self.view = [i for i, x in enumerate(items) if x.acts]
            elif name == "nodate":
                self.view = [i for i, x in enumerate(items) if x.status == plan_mod.NODATE]
            elif name == "dup":
                idx = [i for i, x in enumerate(items) if x.status == plan_mod.DUP or x.similar]
                self.view = sorted(idx, key=lambda i: (items[i].similar or 10 ** 9, items[i].key))
            elif name == "failed":
                self.view = [i for i, x in enumerate(items) if x.key in self.fail_reasons]
            else:
                self.view = list(range(len(items)))
        self.table.selected = None
        if keep is not None:
            for n, i in enumerate(self.view):
                if self.plan.items[i].key == keep:
                    self.table.selected = n
                    break
        self.table.set_count(len(self.view))

    def _on_select(self, idx: int) -> None:
        pass

    def _item_at(self, event) -> plan_mod.Item | None:
        idx = self.table.index_at(event.y)
        if idx is None or not self.plan or idx >= len(self.view):
            return None
        self.table.selected = idx
        self.table.refresh()
        return self.plan.items[self.view[idx]]

    def _on_double(self, event) -> None:
        item = self._item_at(event)
        if item is None or self.busy:
            return
        if self.table.tree.identify_column(event.x) == "#2":  # the date column
            self.edit_date(item)
        else:
            open_path(item.primary.path)

    def _on_menu(self, event) -> None:
        item = self._item_at(event)
        if item is None:
            return
        menu = tk.Menu(self.root, tearoff=0)
        busy = "disabled" if self.busy else "normal"
        menu.add_command(label=t("menu_edit_date"), command=lambda: self.edit_date(item), state=busy)
        if item.key in self.overrides.dates:
            menu.add_command(label=t("menu_reset_date"), command=lambda: self._set_date(item, None), state=busy)
        if item.status == plan_mod.DUP:
            menu.add_command(label=t("menu_mark_keep"), command=lambda: self._set_dup(item, False), state=busy)
        else:
            menu.add_command(label=t("menu_mark_dup"), command=lambda: self._set_dup(item, True), state=busy)
        if item.checked:
            menu.add_command(label=t("menu_exclude"), command=lambda: self._set_checked(item, False), state=busy)
        else:
            menu.add_command(label=t("menu_include"), command=lambda: self._set_checked(item, True), state=busy)
        menu.add_separator()
        menu.add_command(label=t("menu_open_folder"), command=lambda: open_path(item.primary.path, select=True))
        menu.tk_popup(event.x_root, event.y_root)

    def edit_date(self, item: plan_mod.Item) -> None:
        start = item.when.strftime("%Y-%m-%d %H:%M") if item.when else ""
        text = simpledialog.askstring(t("dlg_date_title"), t("dlg_date_prompt", name=item.primary.name),
                                      initialvalue=start, parent=self.root)
        if text is None:
            return
        when = parse_date(text)
        if when is None:
            messagebox.showwarning(t("dlg_date_title"), t("err_bad_date"), parent=self.root)
            return
        self._set_date(item, when)

    def _set_date(self, item: plan_mod.Item, when: datetime.datetime | None) -> None:
        if when is None:
            self.overrides.dates.pop(item.key, None)
        else:
            self.overrides.dates[item.key] = when
        self.request_plan()

    def _set_dup(self, item: plan_mod.Item, dup: bool) -> None:
        self.overrides.dup[item.key] = dup
        self.request_plan()

    def _set_checked(self, item: plan_mod.Item, on: bool) -> None:
        if on:
            self.overrides.unchecked.discard(item.key)
        else:
            self.overrides.unchecked.add(item.key)
        self.request_plan()

    def apply_suggestions(self) -> None:
        for item in self.plan.items if self.plan else []:
            if item.suggest_dup:
                self.overrides.dup[item.key] = True
        self.request_plan()

    def _view_changed(self, _e=None) -> None:
        names = [t(f"view_{v}") for v in VIEWS]
        self.view_name = VIEWS[names.index(self.view_box.get())]
        self._refresh_view()

    def show_failed(self) -> None:
        self.view_name = "failed"
        self._show_top()
        self._refresh_view()

    # -------------------------------------------------------------- options
    def _toggle(self, name: str, var: tk.BooleanVar) -> None:
        setattr(self.prefs, name, bool(var.get()))
        prefs_mod.save(self.prefs)
        self.request_plan()

    def _mtime_changed(self) -> None:
        self.prefs.use_mtime = bool(self.v_mtime.get())
        prefs_mod.save(self.prefs)
        self.request_plan()

    def _pattern_changed(self) -> None:
        text = self.pattern_box.get()
        heic_only = text == t("mode_heic_only")  # the last choice of the list: convert, organize nothing
        if heic_only != self.heic_only.get():
            self.heic_only.set(heic_only)
            self._mode_widgets()
        if heic_only:
            self.pattern_error.pack_forget()
            self.request_plan()
            return
        problem = plan_mod.validate_pattern(text)
        if problem:
            self.pattern_error.configure(text=t(problem))
            self.pattern_error.pack(side="top", anchor="w", before=self.table)
            self.serial += 1  # the shown plan no longer matches what is typed: Run waits
            self._update_buttons()
            return
        self.pattern_error.pack_forget()
        if text != self.prefs.pattern:
            self.prefs.pattern = text
            prefs_mod.save(self.prefs)
        self.request_plan()

    def _mode_widgets(self) -> None:
        """Convert only: HEIC -> JPG is on by definition, duplicates are not looked for."""
        state = "disabled" if self.heic_only.get() else "normal"
        for w in (self.cb_dedupe, self.cb_similar):
            w.configure(state=state)
        # "the JPG of a HEIC pair is a duplicate" (Settings) excludes converting (LESSONS A14)
        self.cb_convert.configure(state="disabled" if self.heic_only.get() or self.prefs.jpg_pair_as_dupe else "normal")

    def _similar_changed(self) -> None:
        on = bool(self.v_similar.get())
        self.prefs.similar = on
        prefs_mod.save(self.prefs)
        if on and self.plan and self.similar_map is None and not self.busy:
            photos = [i for i in self.plan.items if i.primary.kind in dedupe.SIMILAR_KINDS]
            minutes = max(1, round(dedupe.estimate_seconds(photos) / 60))
            if not messagebox.askyesno(t("app_title"), t("msg_similar_estimate", min=minutes), parent=self.root):
                self.v_similar.set(False)
                self.prefs.similar = False
                prefs_mod.save(self.prefs)
                return
            self._start_similar()
            return
        self.request_plan()

    def _thumbs_changed(self) -> None:
        on = bool(self.v_thumbs.get())
        if on and self.scan_result and len(self.scan_result.media) > THUMB_WARN_COUNT:
            if not messagebox.askyesno(t("app_title"), t("msg_thumbs_many", count=fmt_count(THUMB_WARN_COUNT)),
                                       parent=self.root):
                self.v_thumbs.set(False)
                return
        self.prefs.thumbnails = on
        prefs_mod.save(self.prefs)
        self.table.set_thumbs(on)

    def choose_dest(self) -> None:
        folder = filedialog.askdirectory(parent=self.root, initialdir=self.dest or self.source or None)
        if folder:
            self.dest = os.path.abspath(folder)
            self._update_dest_button()
            self.request_plan()

    def reset_dest(self) -> None:
        self.dest = ""
        self._update_dest_button()
        self.request_plan()

    def _update_dest_button(self) -> None:
        """"Destination: <folder name>" in one button (right-click: back to the source folder)."""
        if not self.dest or (self.source and scan_mod.key_of(self.dest) == scan_mod.key_of(self.source)):
            where = t("opt_dest_same")
        else:
            where = os.path.basename(self.dest.rstrip("\\/")) or self.dest
        label = f"{t('opt_dest')}: "
        # shorten the folder name, never the label ("Destinat...folder" read badly)
        self.dest_btn.configure(text=label + short(where, max(8, DEST_CHARS + 4 - len(label))))

    def _on_lang(self, _e=None) -> None:
        names = [i18n.LANG_NAMES[c] for c in i18n.LANGS]
        code = i18n.LANGS[names.index(self.lang_box.get())]
        if code == i18n.current_lang():
            return
        i18n.set_lang(code)
        self._build()
        self.request_plan()  # folder names (No Date, duplicates) come from the language

    # -------------------------------------------------------------- state
    def ui(self) -> Ui:
        return Ui(self.source, self.dest or self.source, self.prefs.mode, bool(self.heic_only.get()),
                  bool(self.v_convert.get()), bool(self.v_dedupe.get()),
                  bool(self.v_similar.get()) and self.similar_map is not None)

    def plan_ready(self) -> bool:
        return self.plan is not None and self.plan_serial == self.serial and not self.busy and self.replan_job is None

    def _update_buttons(self) -> None:
        if self.busy in ("run", "undo"):
            self.run_btn.configure(text=t("btn_stop"), state="normal" if self.busy == "run" else "disabled")
            self.undo_btn.configure(state="disabled")
            return
        self.run_btn.configure(text=t("btn_run"))
        ok = self.plan_ready() and self.plan.actionable() > 0 and not self.plan.blocked_groups()
        if ok and self.plan.options.mode == plan_mod.COPY and \
                scan_mod.key_of(self.plan.options.dest) == scan_mod.key_of(self.plan.options.root):
            ok = False
        self.run_btn.configure(state="normal" if ok else "disabled")
        self.undo_btn.configure(state="normal" if (not self.busy and self._pending_log()) else "disabled")

    def _pending_log(self) -> str | None:
        if not self.source:
            return None
        log = undo_mod.find_log(self.dest or self.source) or undo_mod.find_log(self.source)
        return log if undo_mod.pending_run(log) else None

    # -------------------------------------------------------------- workers
    def _post(self, kind: str, payload=None) -> None:
        self.queue.put((kind, payload))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                getattr(self, f"_on_{kind}")(payload)
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll)

    def _start(self, busy: str, target, *args) -> None:
        self.busy = busy
        self.cancel.clear()
        self._update_buttons()
        threading.Thread(target=target, args=args, daemon=True).start()

    def browse(self) -> None:
        if self.busy:
            return
        folder = filedialog.askdirectory(parent=self.root, initialdir=self.source or None)
        if folder:
            self.open_folder(folder)

    def _on_drop(self, event) -> None:
        if self.busy:
            return
        paths = self.root.tk.splitlist(event.data)
        if not paths:
            return
        p = paths[0]
        self.open_folder(p if os.path.isdir(p) else os.path.dirname(p))

    def open_folder(self, folder: str) -> None:
        if self.busy or not os.path.isdir(folder):
            return
        self.source = os.path.abspath(folder)
        self.path_label.configure(text=short(self.source, 80))
        self.scan_result, self.plan, self.similar_map = None, None, None
        self.overrides = plan_mod.Overrides()
        self.fail_reasons = {}
        self.view_name = "all"
        self.v_similar.set(False)
        self._show_band("")
        self._update_dest_button()
        self._refresh_view()
        self._rescan()

    def _rescan(self) -> None:
        ui = self.ui()
        self._start("scan", self._scan_worker, ui)
        self._show_top()

    def _scan_worker(self, ui: Ui) -> None:
        """Worker: scan, then build the plan. Touches no widget."""
        def progress(done: int, total: int) -> None:
            self._post("scan_progress", (done, total))

        try:
            opts = prefs_mod.options(self.prefs, ui.source, ui.dest, ui.mode, ui.heic_only, ui.convert)
            result = scan_mod.scan(ui.source, prefs_mod.special_folders(ui.source, opts.dest), progress=progress,
                                   cancel=self.cancel, cache=self.cache)
            self._post("scanned", result)
        except Exception as exc:  # unexpected: show it rather than hang
            self._post("worker_error", exc)

    def _on_scan_progress(self, payload) -> None:
        done, total = payload
        if hasattr(self, "scan_bar") and self.scan_bar.winfo_exists():
            self.scan_bar.configure(maximum=max(total, 1), value=done)
            self.scan_label.configure(text=t("msg_scanning", done=fmt_count(done), total=fmt_count(total)))

    def _on_scanned(self, result: scan_mod.ScanResult) -> None:
        self.busy = ""
        if result.cancelled:  # keep whatever was shown before
            self._show_top()
            self._update_buttons()
            return
        self.scan_result = result
        if self.prefs.thumbnails and len(result.media) > THUMB_WARN_COUNT and \
                messagebox.askyesno(t("app_title"), t("msg_thumbs_off", count=fmt_count(THUMB_WARN_COUNT)),
                                    parent=self.root):
            self.prefs.thumbnails = False  # many photos: thumbnails slow the table down
            prefs_mod.save(self.prefs)
            self.table.set_thumbs(False)
        self.thumbs.clear()
        self._show_top()
        self.request_plan(now=True)

    def _on_worker_error(self, exc) -> None:
        self.busy = ""
        self._show_band("error", f"{exc.__class__.__name__}: {exc}")
        self._show_top()
        self._update_buttons()

    def request_plan(self, now: bool = False) -> None:
        """Settings changed: the shown plan is stale until the new one arrives (Run waits)."""
        self.serial += 1
        self._update_buttons()
        if self.replan_job is not None:
            self.root.after_cancel(self.replan_job)
            self.replan_job = None
        if self.scan_result is None:
            return
        self.replan_job = self.root.after(1 if now else REPLAN_DELAY_MS, self._replan)

    def _replan(self) -> None:
        self.replan_job = None
        if self.busy or self.scan_result is None:
            if self.busy in ("plan",):
                self.replan_job = self.root.after(REPLAN_DELAY_MS, self._replan)
            return
        ui = self.ui()
        ov = plan_mod.Overrides(dict(self.overrides.dates), dict(self.overrides.dup), set(self.overrides.unchecked))
        p = dataclasses.replace(self.prefs, name_patterns=list(self.prefs.name_patterns))
        sim = dict(self.similar_map) if (ui.similar and self.similar_map) else {}
        self._start("plan", self._plan_worker, self.serial, ui, p, ov, sim, self.scan_result)

    def _plan_worker(self, serial: int, ui: Ui, p: prefs_mod.Prefs, ov: plan_mod.Overrides, sim: dict,
                     result: scan_mod.ScanResult) -> None:
        try:
            opts = prefs_mod.options(p, ui.source, ui.dest, ui.mode, ui.heic_only, ui.convert)
            dupes, grouped = {}, None
            if ui.dedupe and not ui.heic_only:
                grouped = plan_mod.group_items(result, opts, ov)
                dupes = dedupe.find_exact(grouped, cancel=self.cancel)
            final = plan_mod.build(result, opts, dupes, sim if not ui.heic_only else {}, ov, grouped=grouped)
            self._post("planned", (serial, final))
        except Exception as exc:
            self._post("worker_error", exc)

    def _on_planned(self, payload) -> None:
        serial, the_plan = payload
        self.busy = ""
        self.plan = the_plan
        self.plan_serial = serial
        self._fill_summary()
        self._show_top()
        self._refresh_view()
        blocked = the_plan.blocked_groups()
        if blocked:
            self._show_band("error", t("msg_blocked_group"))
        elif self.band_kind == "error":
            self._show_band("")
        self._update_buttons()

    def _start_similar(self) -> None:
        items = list(self.plan.items)
        threshold = self.prefs.similar_threshold
        self._start("similar", self._similar_worker, items, threshold)
        self._show_top()
        self.scan_label.configure(text=t("msg_similar_started"))

    def _similar_worker(self, items, threshold: int) -> None:
        def progress(done: int, total: int) -> None:
            self._post("scan_progress_similar", (done, total))

        try:
            self._post("similar_done", dedupe.find_similar(items, threshold, progress=progress, cancel=self.cancel))
        except Exception as exc:
            self._post("worker_error", exc)

    def _on_scan_progress_similar(self, payload) -> None:
        done, total = payload
        if hasattr(self, "scan_bar") and self.scan_bar.winfo_exists():
            self.scan_bar.configure(maximum=max(total, 1), value=done)
            self.scan_label.configure(text=t("msg_similar_progress", done=fmt_count(done), total=fmt_count(total)))

    def _on_similar_done(self, result: dict) -> None:
        self.busy = ""
        if self.cancel.is_set():
            self.v_similar.set(False)
            self.prefs.similar = False
        else:
            self.similar_map = result
            self.view_name = "dup" if result else self.view_name
        self._show_top()
        self.request_plan(now=True)

    # -------------------------------------------------------------- run
    def run_or_stop(self) -> None:
        if self.busy == "run":
            self.cancel.set()
            return
        if not self.plan_ready():
            return
        the_plan = self.plan
        need, free = mover.space_check(the_plan)
        if need > free:
            self._show_band("error", t("msg_no_space", need=core.human_size(need), free=core.human_size(free)))
            return
        self._show_band("")
        self.fail_reasons = {}
        self.view_name = "all"
        self._start("run", self._run_worker, the_plan, self.prefs.quality, self.prefs.icc)
        self._show_footer_mode()
        self.progress.configure(value=0, maximum=1)

    def _run_worker(self, the_plan: plan_mod.Plan, quality: int, icc: str) -> None:
        def progress(done: int, total: int) -> None:
            self._post("run_progress", (done, total))

        def on_file(path: str) -> None:
            self._post("run_file", path)

        try:
            res = mover.execute(the_plan, progress=progress, cancel=self.cancel, quality=quality, icc=icc,
                                on_file=on_file)
            self._post("ran", (the_plan, res, ""))
        except OSError as exc:  # the log could not be written: nothing moved
            self._post("ran", (the_plan, None, f"{t('err_log_write')} ({exc})"))
        except Exception as exc:
            self._post("worker_error", exc)

    def _on_run_progress(self, payload) -> None:
        done, total = payload
        self.progress.configure(maximum=max(total, 1), value=done)

    def _on_run_file(self, path: str) -> None:
        self.current_label.configure(text=short(rel(path, self.source), 90))

    def _on_ran(self, payload) -> None:
        the_plan, res, error = payload
        self.busy = ""
        self._show_footer_mode()
        if res is None:
            self._show_band("error", error)
            self._update_buttons()
            return
        self.last_result = res
        by_key = {}
        for item in the_plan.items:
            for m in item.members:
                by_key[scan_mod.key_of(m.path)] = item.key
        self.fail_reasons = {by_key.get(scan_mod.key_of(mover.plain(p)), scan_mod.key_of(p)): r for p, r in res.failed}
        log_errors = [r for p, r in res.failed if scan_mod.key_of(mover.plain(p)) == scan_mod.key_of(res.log_path)]
        log_lost = t("err_log_lost") in log_errors
        # "log saved" is part of the done message: never say it when the log could not be saved
        text = t("err_log_write") if log_lost else t("msg_done", move=fmt_count(res.done),
                                                      convert=fmt_count(res.converted))
        if res.converted and not log_lost:
            text += f"  {DOT}  " + t("msg_size_change", before=core.human_size(res.bytes_in),
                                       after=core.human_size(res.bytes_out))
        if res.cancelled:
            text += f"  {DOT}  " + t("msg_cancelled")
        if res.failed:
            text += f"  {DOT}  " + t("msg_failed", count=fmt_count(len(res.failed)))
            if log_errors:
                text += "\n" + log_errors[0]
            if log_lost:
                self._show_moved_list(res)  # nothing on the disk records these moves: show them
            self._show_band("failed", text)
        else:
            self._show_band("done", text)
        self.overrides = plan_mod.Overrides()
        self.similar_map = None
        self.v_similar.set(False)
        self._rescan()

    def _show_moved_list(self, res: mover.Result) -> None:
        """The log could not be saved: show what moved, so it can be put back by hand."""
        win = tk.Toplevel(self.root)
        win.title(t("msg_moved_list"))
        txt = tk.Text(win, width=100, height=24, font=self.f_table)
        txt.pack(fill="both", expand=True)
        txt.insert("end", "\n".join(f"{s}  ->  {d}" for s, d in res.moved_paths))

    def undo(self) -> None:
        if self.busy:
            return
        log = self._pending_log()
        run = undo_mod.pending_run(log)
        if not log or not run:
            self._show_band("error", t("msg_no_log"))
            return
        count = sum(1 for op in run.get("ops", []) if op.get("op") in ("move", "copy", "convert", "trash"))
        if not messagebox.askyesno(t("btn_undo"), t("msg_undo_confirm", time=str(run.get("time", "")).replace("T", " "),
                                                   count=fmt_count(count)), parent=self.root):
            return
        logs = undo_mod.candidate_logs(self.source, self.dest or None)
        self._start("undo", self._undo_worker, log, logs)
        self._show_footer_mode()

    def _undo_worker(self, log: str, logs: list[str]) -> None:
        def progress(done: int, total: int) -> None:
            self._post("run_progress", (done, total))

        try:
            self._post("undone", undo_mod.undo(log, progress=progress, cancel=self.cancel, other_logs=logs))
        except Exception as exc:
            self._post("worker_error", exc)

    def _on_undone(self, res: undo_mod.UndoResult) -> None:
        self.busy = ""
        self._show_footer_mode()
        if res.blocked_by:
            first = res.blocked_by[0]
            self._show_band("error", t("msg_undo_blocked", time=first.time.replace("T", " "), id=first.id))
        elif res.nothing:
            self._show_band("error", t("msg_no_log"))
        elif res.log_error:
            self._show_band("error", res.log_error)
        else:
            text = t("msg_undo_done", count=fmt_count(res.restored))
            if res.skipped or res.log_unsaved:
                text += f"  {DOT}  " + t("msg_failed", count=fmt_count(len(res.skipped)))
                self.fail_reasons = {scan_mod.key_of(p): r for p, r in res.skipped}
                self._show_band("failed" if res.skipped else "error", text + ("\n" + res.log_unsaved if res.log_unsaved else ""))
            else:
                self._show_band("done", text)
        self._rescan()

    def open_dest(self) -> None:
        open_path(self.dest or self.source)

    # -------------------------------------------------------------- settings
    def open_settings(self) -> None:
        SettingsWindow(self)

    def settings_changed(self, name: str) -> None:
        prefs_mod.save(self.prefs)
        if name == "theme":
            self._apply_theme()
            self._build()
            return
        if name == "dest":
            self.dest = self.prefs.dest
            self._update_dest_button()
        if name == "use_mtime":
            self.v_mtime.set(self.prefs.use_mtime)
        if name == "jpg_pair_as_dupe":
            self._mode_widgets()
        if name == "similar_threshold":
            self.similar_map = None
            self.v_similar.set(False)
        self.request_plan()

    # thumbnails arrive from the loader thread
    def _on_thumb(self, payload) -> None:
        path, pil = payload
        self.thumbs.put(path, pil)
        if self.thumb_refresh is None:
            self.thumb_refresh = self.root.after(100, self._thumb_redraw)

    def _thumb_redraw(self) -> None:
        self.thumb_refresh = None
        self.table.refresh()


# ------------------------------------------------------------------ settings window
class SettingsWindow:
    """Three tabs; every change is saved at once (no OK button)."""

    def __init__(self, app: App) -> None:
        self.app = app
        p = app.prefs
        win = tk.Toplevel(app.root)
        self.win = win
        win.title(t("set_title"))
        win.transient(app.root)
        win.resizable(False, False)
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=px(theme.PAD_OUT), pady=px(theme.PAD_OUT))
        self.vars: dict[str, tk.Variable] = {}
        general = ttk.Frame(nb, padding=px(theme.PAD) * 2)
        dates_tab = ttk.Frame(nb, padding=px(theme.PAD) * 2)
        conv = ttk.Frame(nb, padding=px(theme.PAD) * 2)
        nb.add(general, text=t("tab_general"))
        nb.add(dates_tab, text=t("tab_dates"))
        nb.add(conv, text=t("tab_convert"))

        row = self._row(general, t("set_lang"))
        names = [i18n.LANG_NAMES[code] for code in i18n.LANGS]
        lang_box = ttk.Combobox(row, values=names, state="readonly", width=12, font=app.f_body)
        lang_box.set(i18n.LANG_NAMES[i18n.current_lang()])

        def on_lang(_e=None) -> None:
            app.lang_box.set(lang_box.get())
            win.destroy()  # its labels are in the old language; the main window rebuilds
            app._on_lang()

        lang_box.bind("<<ComboboxSelected>>", on_lang)
        lang_box.pack(side="left")
        row = self._row(general, t("set_theme"))
        for value in ("light", "dark", "system"):
            self._radio(row, "theme", value, t(f"theme_{value}"))
        row = self._row(general, t("set_mode"))
        for value in (plan_mod.MOVE, plan_mod.COPY):
            self._radio(row, "mode", value, t(f"mode_{value}"))
        row = self._row(general, t("set_dest_default"))
        self.dest_label = ttk.Label(row, text=short(p.dest, 40) if p.dest else t("opt_dest_same"), width=36)
        self.dest_label.pack(side="left")
        ttk.Button(row, text=t("btn_browse"), command=self._pick_dest).pack(side="left", padx=px(theme.PAD))
        ttk.Button(row, text=t("btn_clear"), command=self._clear_dest).pack(side="left")
        self._check(general, "rename", t("set_rename"))
        self._check(general, "include_nodate", t("set_include_nodate"))
        self._check(general, "remove_empty", t("set_remove_empty"))
        row = self._row(general, t("set_dupes_action"))
        for value in ("move", "trash"):
            self._radio(row, "dupes_action", value, t(f"dupes_{value}"))
        self._check(general, "jpg_pair_as_dupe", t("set_jpg_pair_dupe"))

        self._check(dates_tab, "use_mtime", t("opt_use_mtime"))
        row = self._row(dates_tab, t("set_year_range"))
        self._spin(row, "year_min", 1900, 2100)
        ttk.Label(row, text=" ~ ").pack(side="left")
        self._spin(row, "year_max", 0, 2100)
        ttk.Label(row, text=t("set_year_max_hint"), style="Muted.TLabel").pack(side="left", padx=px(theme.PAD))
        ttk.Label(dates_tab, text=t("set_name_patterns")).pack(anchor="w", pady=(px(theme.PAD) * 2, 0))
        ttk.Label(dates_tab, text=t("set_name_patterns_hint"), style="Muted.TLabel", wraplength=px(520),
                  justify="left").pack(anchor="w")
        self.patterns_text = tk.Text(dates_tab, height=5, width=64, font=app.f_table)
        self.patterns_text.insert("1.0", "\n".join(p.name_patterns))
        self.patterns_text.pack(anchor="w", pady=(px(theme.PAD), 0))
        self.patterns_text.bind("<KeyRelease>", lambda e: self._patterns_changed())
        self.patterns_error = ttk.Label(dates_tab, text="", foreground=theme.ERR_TEXT)
        self.patterns_error.pack(anchor="w")

        row = self._row(conv, t("set_quality"))
        self.vars["quality"] = tk.IntVar(value=p.quality)
        q_label = ttk.Label(row, text=str(p.quality), width=4)

        def on_quality(v) -> None:
            value = int(float(v))
            q_label.configure(text=str(value))
            if value != app.prefs.quality:
                app.prefs.quality = value
                app.settings_changed("quality")

        ttk.Scale(row, from_=80, to=100, variable=self.vars["quality"], command=on_quality, length=px(200)).pack(side="left")
        q_label.pack(side="left", padx=px(theme.PAD))
        row = self._row(conv, t("set_icc"))
        for value in ("keep", "srgb"):
            self._radio(row, "icc", value, t(f"icc_{value}"))
        row = self._row(conv, t("set_heic_original"))
        for value in ("keep", "move"):
            self._radio(row, "heic_original", value, t(f"heic_{value}"))
        import convert as convert_mod

        cb = self._check(conv, "convert_avif", t("set_convert_avif"))
        if not convert_mod.avif_supported():
            cb.configure(state="disabled", text=t("set_convert_avif") + f" ({t('set_avif_unavailable')})")
        row = self._row(conv, t("set_similar_threshold"))
        self._spin(row, "similar_threshold", 0, 16)

    def _row(self, parent, label: str) -> ttk.Frame:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(px(theme.PAD), 0))
        ttk.Label(row, text=label, width=22).pack(side="left")
        return row

    def _set(self, name: str, value) -> None:
        if getattr(self.app.prefs, name) == value:
            return
        setattr(self.app.prefs, name, value)
        self.app.settings_changed(name)

    def _radio(self, parent, name: str, value: str, text: str) -> None:
        var = self.vars.setdefault(name, tk.StringVar(value=getattr(self.app.prefs, name)))
        ttk.Radiobutton(parent, text=text, variable=var, value=value,
                        command=lambda: self._set(name, var.get())).pack(side="left", padx=(0, px(theme.PAD) * 2))

    def _check(self, parent, name: str, text: str) -> ttk.Checkbutton:
        var = tk.BooleanVar(value=getattr(self.app.prefs, name))
        self.vars[name] = var
        cb = ttk.Checkbutton(parent, text=text, variable=var, command=lambda: self._set(name, bool(var.get())))
        cb.pack(anchor="w", pady=(px(theme.PAD), 0))
        return cb

    def _spin(self, parent, name: str, lo: int, hi: int) -> None:
        var = tk.StringVar(value=str(getattr(self.app.prefs, name)))
        self.vars[name] = var

        def changed(*_a) -> None:
            try:
                value = int(var.get())
            except ValueError:
                return
            if lo <= value <= hi:
                self._set(name, value)

        var.trace_add("write", changed)
        ttk.Spinbox(parent, from_=lo, to=hi, textvariable=var, width=6).pack(side="left")

    def _pick_dest(self) -> None:
        folder = filedialog.askdirectory(parent=self.win)
        if folder:
            self.dest_label.configure(text=short(os.path.abspath(folder), 40))
            self._set("dest", os.path.abspath(folder))

    def _clear_dest(self) -> None:
        self.dest_label.configure(text=t("opt_dest_same"))
        self._set("dest", "")

    def _patterns_changed(self) -> None:
        lines = [x.strip() for x in self.patterns_text.get("1.0", "end").splitlines() if x.strip()]
        bad = prefs_mod.patterns.compile_user(lines)[1]
        self.patterns_error.configure(text=t("err_bad_regex", patterns=", ".join(bad)) if bad else "")
        good = [x for x in lines if x not in bad]
        if good != self.app.prefs.name_patterns:
            self._set("name_patterns", good)


def _icon(root: tk.Tk) -> None:
    path = os.path.join(i18n.resource_dir(), "assets", "icon.png")
    try:
        img = tk.PhotoImage(file=path)
        root.iconphoto(True, img)
        root._icon = img  # keep a reference
    except tk.TclError:
        pass


def size_window(root: tk.Tk) -> None:
    """PRD sizes are design pixels: grow them with the screen scaling, never past the screen."""
    theme.set_scale(root.winfo_fpixels("1i"))
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w, h = min(px(theme.WINDOW[0]), sw), min(px(theme.WINDOW[1]), sh - px(48))
    root.geometry(f"{w}x{h}")
    root.minsize(min(px(theme.WINDOW_MIN[0]), sw), min(px(theme.WINDOW_MIN[1]), sh - px(48)))


def launch(folder: str | None = None) -> None:
    _enable_dpi_awareness()
    root = TkinterDnD.Tk() if _HAS_DND else tk.Tk()
    size_window(root)
    _icon(root)
    App(root, folder)
    root.mainloop()
