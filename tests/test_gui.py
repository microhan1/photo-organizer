"""GUI behaviour and layout. One Tk root for the whole module (repeated Tk() is flaky on
Windows). No screen-coordinate hit tests: they fail while the PC is locked. Layout is
checked from widget geometry; look at real screenshots too (tests/shots.py)."""
from __future__ import annotations

import errno
import os
import time
import tkinter as tk

import pytest
from conftest import ms, snapshot, without_log

import i18n
import mover
import plan as plan_mod
import theme

gui = pytest.importorskip("gui")


@pytest.fixture(scope="module")
def root():
    r = gui.TkinterDnD.Tk() if gui._HAS_DND else tk.Tk()
    r.geometry(f"{theme.WINDOW[0]}x{theme.WINDOW[1]}+10+10")
    r.minsize(*theme.WINDOW_MIN)
    yield r
    r.destroy()


@pytest.fixture
def app(root):
    i18n.set_lang("en", persist=False)
    for child in root.winfo_children():
        child.destroy()
    a = gui.App(root)
    yield a
    a.cancel.set()
    pump(root, lambda: not a.busy, 30)
    if a.replan_job is not None:  # nothing scheduled may touch this App's widgets after the next test rebuilds
        root.after_cancel(a.replan_job)
    a._poll = lambda: None


def pump(root, cond, timeout=60.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.01)
    return False


def ready(app) -> bool:
    return app.plan is not None and not app.busy and app.replan_job is None and app.plan_serial == app.serial


def load(app, folder) -> None:
    app.open_folder(str(folder))
    assert pump(app.root, lambda: ready(app)), "plan never arrived"


def fully_visible(w, root) -> bool:
    """Mapped, inside the window, and not squeezed below the size it asked for."""
    root.update_idletasks()
    if not w.winfo_ismapped():
        return False
    x = w.winfo_rootx() - root.winfo_rootx()
    y = w.winfo_rooty() - root.winfo_rooty()
    inside = x >= 0 and y >= 0 and x + w.winfo_width() <= root.winfo_width() and y + w.winfo_height() <= root.winfo_height()
    return inside and w.winfo_width() >= w.winfo_reqwidth() - 1


# ------------------------------------------------------------------ layout
@pytest.mark.parametrize("lang", ["en", "ko", "zh-CN", "ja"])
@pytest.mark.parametrize("size", [theme.WINDOW, theme.WINDOW_MIN])
def test_every_control_fits_in_every_language(app, root, sample_folder, lang, size):
    root.geometry(f"{size[0]}x{size[1]}")
    i18n.set_lang(lang, persist=False)
    app._build()
    load(app, sample_folder)
    root.update()
    controls = [app.run_btn, app.undo_btn, app.dest_btn, app.pattern_box, app.cb_convert, app.cb_dedupe,
                app.cb_similar, app.lang_box]
    controls += list(app.options_row.winfo_children())
    controls += [w for f in (app.sum_line, app.sum_line2) for w in f.pack_slaves()]  # what the bar shows now
    assert app.mtime_cb is not None and app.mtime_cb.winfo_manager()  # the samples have files without a date
    bad = [str(w) for w in controls if not fully_visible(w, root)]
    assert bad == [], (lang, size, bad)
    assert app.table.winfo_height() > 3 * theme.ROW_HEIGHT


def test_summary_reflow_settles_at_every_width(app, root, sample_folder):
    """The "use modified time" toggle once flipped between the two lines forever while the window
    was being resized (it counted its own width twice): the window froze."""
    load(app, sample_folder)
    for width in range(theme.WINDOW_MIN[0], theme.WINDOW[0] + 1, 6):
        root.geometry(f"{width}x{theme.WINDOW_MIN[1]}")
        assert pump(root, lambda: True, 1)
        for _ in range(3):
            root.update()
        first = app.mtime_cb.pack_info().get("in")
        for _ in range(3):
            app._reflow_summary()
            root.update()
        assert app.mtime_cb.pack_info().get("in") == first, width


def test_run_button_is_bottom_right(app, root, sample_folder):
    load(app, sample_folder)
    root.update()
    x = app.run_btn.winfo_rootx() - root.winfo_rootx() + app.run_btn.winfo_width()
    y = app.run_btn.winfo_rooty() - root.winfo_rooty() + app.run_btn.winfo_height()
    assert root.winfo_width() - x <= theme.PAD_OUT + 2
    assert root.winfo_height() - y <= theme.PAD_OUT + 2


def test_drop_area_before_and_summary_strip_after(app, root, sample_folder):
    root.update()
    assert any(isinstance(w, tk.Canvas) for w in app.top_area.winfo_children())
    load(app, sample_folder)
    assert not any(isinstance(w, tk.Canvas) for w in app.top_area.winfo_children())
    labels = [w.cget("text") for f in app.top_area.winfo_children() for w in f.winfo_children()
              if isinstance(w, gui.ttk.Label)]
    assert any("14 files" in text for text in labels)


# ------------------------------------------------------------------ table
def test_table_rows_have_text_and_colour(app, sample_folder):
    load(app, sample_folder)
    tree = app.table.tree
    rows = [tree.item(i) for i in tree.get_children()]
    assert rows
    statuses = {str(r["values"][4]) for r in rows}
    assert "OK" in statuses and any(s.startswith("No date") for s in statuses)
    for r in rows:
        tag = r["tags"][0]
        assert tag in theme.STATUS_COLORS or tag == "off"
        assert r["values"][4]  # the status is always written out, never colour only


def test_virtual_table_holds_only_visible_rows(app, root, tmp_path):
    folder = tmp_path / "in1"
    data = open(ms.photo(str(tmp_path / "t.jpg"), size=(16, 12)), "rb").read()
    for n in range(3000):
        d = folder / f"d{n // 500}"
        os.makedirs(d, exist_ok=True)
        (d / f"IMG_202401{1 + n % 28:02d}_101010_{n}.jpg").write_bytes(data + n.to_bytes(4, "big"))
    load(app, folder)
    assert app.table.count == 3000
    assert len(app.table.tree.get_children()) <= app.table.visible <= 40
    app.table.select(2999)
    root.update()
    assert str(2999) in app.table.tree.get_children()
    assert app.table.tree.selection() == (str(2999),)


def test_selection_survives_a_replan(app, root, sample_folder):
    load(app, sample_folder)
    app.table.select(3)
    key = app.plan.items[app.view[3]].key
    app.request_plan(now=True)
    assert pump(root, lambda: ready(app))
    assert app.plan.items[app.view[app.table.selected]].key == key


def test_filter_views(app, sample_folder):
    load(app, sample_folder)
    app.view_name = "nodate"
    app._refresh_view()
    assert app.table.count == sum(1 for i in app.plan.items if i.status == plan_mod.NODATE) == 3
    app.view_name = "todo"
    app._refresh_view()
    assert app.table.count == sum(1 for i in app.plan.items if i.acts)


# ------------------------------------------------------------------ run gating (organizer A23)
def test_run_waits_for_a_plan_made_with_the_current_settings(app, root, sample_folder):
    load(app, sample_folder)
    assert str(app.run_btn.cget("state")) == "normal"
    app.pattern_box.set("{yyyy-mm-dd}")
    app._pattern_changed()
    assert str(app.run_btn.cget("state")) == "disabled"  # until the new plan is there
    assert pump(root, lambda: ready(app))
    assert str(app.run_btn.cget("state")) == "normal"
    assert os.path.basename(os.path.dirname(app.plan.items[0].dst)) == app.plan.items[0].when.strftime("%Y-%m-%d")


def test_convert_only_from_the_pattern_list(app, root, sample_folder):
    load(app, sample_folder)
    app.pattern_box.set(i18n.t("mode_heic_only"))
    app._pattern_changed()
    assert pump(root, lambda: ready(app))
    assert app.plan.options.heic_only
    assert str(app.cb_dedupe.cget("state")) == "disabled" and str(app.cb_convert.cget("state")) == "disabled"
    item = next(i for i in app.plan.items if i.primary.name == "IMG_0001.HEIC")
    assert item.convert_dst == str(sample_folder / "IMG_0001.jpg") and not item.moves
    assert app.plan.summary()["move"] == 0
    app.lang_box.set(i18n.LANG_NAMES["ja"])
    app._on_lang()  # the choice survives a language switch, in the new language
    assert app.pattern_box.get() == i18n.t("mode_heic_only") and app.heic_only.get()
    app.pattern_box.set(app.prefs.pattern)
    app._pattern_changed()
    assert pump(root, lambda: ready(app))
    assert not app.plan.options.heic_only and str(app.cb_dedupe.cget("state")) == "normal"


def test_bad_pattern_keeps_run_disabled(app, root, sample_folder):
    load(app, sample_folder)
    app.pattern_box.set("{nope}")
    app._pattern_changed()
    root.update()
    assert str(app.run_btn.cget("state")) == "disabled"
    assert app.pattern_error.winfo_ismapped()


def test_nothing_to_do_disables_run(app, root, tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "photo.jpg"))  # no date: stays
    load(app, folder)
    assert str(app.run_btn.cget("state")) == "disabled"


# ------------------------------------------------------------------ run and undo
def test_run_then_undo_restores_everything(app, root, sample_folder, monkeypatch):
    before = snapshot(sample_folder)
    app.v_convert.set(True)
    app.prefs.convert = True
    load(app, sample_folder)
    app.run_or_stop()
    assert app.busy == "run"
    assert str(app.run_btn.cget("text")) == i18n.t("btn_stop")
    assert pump(root, lambda: not app.busy and ready(app), 120)
    assert app.band_kind == "done"
    assert app.plan.actionable() == 0  # rescanned: everything is in place
    assert os.path.exists(sample_folder / "2024" / "2024-03-15" / "IMG_0001.jpg")
    assert str(app.undo_btn.cget("state")) == "normal"
    asked = []
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: asked.append(a) or True)
    app.undo()
    assert pump(root, lambda: not app.busy and ready(app), 120)
    assert asked and app.band_kind == "done"
    assert without_log(snapshot(sample_folder)) == without_log(before)


def test_log_failure_shows_a_red_band_and_moves_nothing(app, root, sample_folder, monkeypatch):
    before = snapshot(sample_folder)
    load(app, sample_folder)
    monkeypatch.setattr(mover, "save_log", lambda p, d: (_ for _ in ()).throw(OSError(errno.EACCES, "denied")))
    app.run_or_stop()
    assert pump(root, lambda: not app.busy, 60)
    assert app.band_kind == "error"
    assert snapshot(sample_folder) == before


def test_lost_log_never_says_saved_and_lists_what_moved(app, root, sample_folder, monkeypatch):
    load(app, sample_folder)
    real_write = mover.RunLog._write
    count = {"n": 0}

    def write(self, obj):
        count["n"] += 1
        if count["n"] > 3:
            raise mover.LogWriteError(errno.ENOSPC, "disk full", "journal")
        return real_write(self, obj)

    saves = {"n": 0}
    real_save = mover.save_log

    def save(path, data):
        saves["n"] += 1
        if saves["n"] > 1:  # the first save (before any move) works, the final one does not
            raise OSError(errno.ENOSPC, "disk full")
        return real_save(path, data)

    monkeypatch.setattr(mover.RunLog, "_write", write)
    monkeypatch.setattr(mover, "save_log", save)
    before = set(root.winfo_children())
    app.run_or_stop()
    assert pump(root, lambda: not app.busy and app.band_kind == "failed", 60)
    assert i18n.t("err_log_write") in app.band_text
    assert "log saved" not in app.band_text
    windows = [w for w in root.winfo_children() if w not in before and isinstance(w, tk.Toplevel)]
    assert windows, "the list of moved files must be shown"
    for w in windows:
        w.destroy()


def test_language_switch_renames_columns_and_folders(app, root, tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "photo.jpg"))
    app.prefs.include_nodate = True
    load(app, folder)
    assert "No Date" in app.plan.items[0].dst
    app.lang_box.set(i18n.LANG_NAMES["ko"])
    app._on_lang()
    assert pump(root, lambda: ready(app))
    assert "날짜 없음" in app.plan.items[0].dst
    assert app.table.tree.heading("date")["text"] == "촬영일"


def test_manual_date_from_the_table(app, root, sample_folder, monkeypatch):
    load(app, sample_folder)
    item = next(i for i in app.plan.items if i.primary.name == "IMG_1234.jpg")
    monkeypatch.setattr(gui.simpledialog, "askstring", lambda *a, **k: "2021-05-05 10:30")
    app.edit_date(item)
    assert pump(root, lambda: ready(app))
    fixed = next(i for i in app.plan.items if i.primary.name == "IMG_1234.jpg")
    assert fixed.basis == "manual" and "2021-05-05" in fixed.dst


def test_settings_window_saves_at_once(app, root, sample_folder):
    import prefs as prefs_mod

    load(app, sample_folder)
    win = gui.SettingsWindow(app)
    win.vars["rename"].set(True)
    win._set("rename", True)
    assert prefs_mod.load().rename is True  # no OK button: saved immediately
    assert pump(root, lambda: ready(app))
    item = next(i for i in app.plan.items if i.primary.name == "IMG-20240322-WA0001.jpg")
    assert os.path.basename(item.dst).startswith("2024-03-22_000000_")
    win.patterns_text.insert("1.0", "(broken\n^Cam_(?P<y>\\d{4})(?P<m>\\d{2})(?P<d>\\d{2})")
    win._patterns_changed()
    assert "(broken" in win.patterns_error.cget("text")
    assert prefs_mod.load().name_patterns == ["^Cam_(?P<y>\\d{4})(?P<m>\\d{2})(?P<d>\\d{2})"]
    win._set("theme", "dark")  # rebuilds the main window in the dark palette
    root.update()
    assert app.colors is theme.DARK
    win._set("theme", "light")
    win.win.destroy()


def test_thumbnail_cache_is_bounded(app):
    from PIL import Image

    for n in range(gui.THUMB_CACHE + 50):
        app.thumbs.put(f"p{n}", Image.new("RGB", (4, 4)))
    assert len(app.thumbs.cache) == gui.THUMB_CACHE


def test_worker_reads_no_tk_variable(app, sample_folder, monkeypatch):
    """organizer A14: everything a worker needs is read before it starts."""
    load(app, sample_folder)
    import threading

    main_thread = threading.main_thread()
    real_get = tk.BooleanVar.get

    def guarded(self):
        assert threading.current_thread() is main_thread, "tk variable read on a worker thread"
        return real_get(self)

    monkeypatch.setattr(tk.BooleanVar, "get", guarded)
    app.request_plan(now=True)
    assert pump(app.root, lambda: ready(app))
