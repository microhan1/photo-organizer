"""[Send feedback], series follow-up of 2026-10-27/28: a Tab stop that Enter and Space open
once, a small note while the mouse rests on it, one rule for which page a language code
gets (feedback and [Update] alike), and the README links. The browser is faked, settings
live in a temporary folder, and nothing may leave the machine (conftest counts it).

Key presses: a generated key event only reaches a widget while the window has the
system's keyboard focus, and taking that focus would take it from whoever is typing. So
press() runs the scripts Tk itself runs for the key - the widget's own binding, then its
class, its window and "all", stopping at "break" - and the Tab order is read with
tk_focusNext / tk_focusPrev, which is what Tk's <<NextWindow>> / <<PrevWindow>> use."""
from __future__ import annotations

import json
import os
import re
import time
import tkinter as tk
import urllib.parse

import pytest
from conftest import ROOT

import i18n
import theme
import update_check as uc
from version import APP_VERSION

gui = pytest.importorskip("gui")

SLUG = "photoorganizer"
KO_PAGE = "https://chaekgalpi.co.kr/tools/photoorganizer"
EN_PAGE = "https://chaekgalpi.co.kr/en/tools/photoorganizer"
TIPS = {"ko": "안 되는 것·불편한 것·바라는 기능을 알려 주세요(브라우저가 열립니다)",
        "en": "Tell us what's broken or missing (opens your browser)",
        "ja": "不具合・ご要望をお知らせください（ブラウザが開きます）",
        "zh-CN": "告诉我们问题或建议（将打开浏览器）"}


def page_of(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


# ====================================================================== c. one language rule
KOREAN = ["ko", "KO", "Ko", " ko ", "ko-KR", "ko_KR", "KO-kr", "ko-Kore-KR", "\tko\n"]
NOT_KOREAN = ["en", "ja", "zh", "zh-CN", "zh_CN", "zh-TW", "fr", "de", "EN", "en-US", "xx", "JA", "zh-cn",
              "en_GB", "pt-BR", "kok", "kor", "korean", "k", "-ko", "_ko", "x-ko", "english"]
MISSING = [None, "", " ", "   ", "\t\n", 7, b"ko", ["ko"]]
UPDATE_KO = "https://chaekgalpi.co.kr/tools/photoorganizer?utm_source=app&utm_medium=update"
UPDATE_EN = "https://chaekgalpi.co.kr/en/tools/photoorganizer?utm_source=app&utm_medium=update"


def info(url_en=UPDATE_EN):
    return uc.UpdateInfo("9.9.9", APP_VERSION, UPDATE_KO, url_en)


@pytest.mark.parametrize("lang", KOREAN)
def test_codes_whose_first_part_is_ko_get_the_korean_page(lang):
    assert uc.is_korean(lang) is True
    assert page_of(uc.feedback_url(SLUG, APP_VERSION, lang)) == KO_PAGE
    assert uc.page_url(info(), lang) == UPDATE_KO


@pytest.mark.parametrize("lang", NOT_KOREAN)
def test_every_other_code_gets_the_english_page(lang):
    assert uc.is_korean(lang) is False
    assert page_of(uc.feedback_url(SLUG, APP_VERSION, lang)) == EN_PAGE
    assert uc.page_url(info(), lang) == UPDATE_EN


def test_the_window_s_own_four_languages():
    assert {code: uc.is_korean(code) for code in i18n.LANGS} == {"ko": True, "en": False, "ja": False,
                                                                 "zh-CN": False}


@pytest.mark.parametrize("lang", MISSING)
def test_no_language_follows_the_default_screen_language(lang, monkeypatch):
    assert i18n.DEFAULT_LANG == "en"  # this program opens in English when it cannot tell
    assert uc.is_korean(lang) is False
    assert page_of(uc.feedback_url(SLUG, APP_VERSION, lang)) == EN_PAGE
    assert uc.page_url(info(), lang) == UPDATE_EN
    monkeypatch.setattr(i18n, "DEFAULT_LANG", "ko")  # the rule really reads the default
    assert uc.is_korean(lang) is True
    assert page_of(uc.feedback_url(SLUG, APP_VERSION, lang)) == KO_PAGE
    assert uc.page_url(info(), lang) == UPDATE_KO


@pytest.mark.parametrize("lang", KOREAN + NOT_KOREAN + MISSING)
@pytest.mark.parametrize("version", [APP_VERSION, "", None])
def test_every_address_has_the_same_shape(lang, version):
    url = uc.feedback_url(SLUG, version, lang)
    p = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qs(p.query)
    assert url.startswith(uc.SITE) and url.endswith("#feedback") and url.count("#") == 1
    assert p.scheme == "https" and p.netloc == "chaekgalpi.co.kr"
    assert p.path in ("/tools/photoorganizer", "/en/tools/photoorganizer")
    assert q.pop("utm_source") == ["app"] and q.pop("utm_medium") == ["feedback"]
    assert q == ({"v": [version]} if version else {})
    assert all(32 < ord(ch) < 127 for ch in url)


@pytest.mark.parametrize("lang", KOREAN + NOT_KOREAN + MISSING)
def test_feedback_and_update_pick_the_same_page(lang):
    korean_feedback = page_of(uc.feedback_url(SLUG, APP_VERSION, lang)) == KO_PAGE
    assert (uc.page_url(info(), lang) == UPDATE_KO) is korean_feedback is uc.is_korean(lang)


@pytest.mark.parametrize("lang", KOREAN + NOT_KOREAN + MISSING)
def test_update_without_an_english_page_still_opens_the_korean_one(lang):
    assert uc.page_url(info(url_en=""), lang) == UPDATE_KO


def test_one_rule_function_decides_both():
    import inspect

    assert "is_korean(lang)" in inspect.getsource(uc.feedback_url)
    assert "is_korean(lang)" in inspect.getsource(uc.page_url)
    assert not hasattr(uc, "FEEDBACK_EN_LANGS")  # the old per-language list is gone


# ====================================================================== d. the READMEs
READMES = {"README.md": "ko", "README.en.md": "en", "README.ja.md": "ja", "README.zh-CN.md": "zh-CN"}
MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)\)")
BARE_URL = re.compile(r"https?://[^\s<>\[\]]+")
BAD_TAIL = re.compile(r"[)\]（）가-힣ㄱ-ㆎ，。、；：！？」』】]")  # ) Hangul full-width marks


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def links(text):
    return [href for _, href in MD_LINK.findall(text)]


@pytest.mark.parametrize("name, lang", READMES.items())
def test_readme_feedback_link_per_language(name, lang):
    found = [h for h in links(read(name)) if h.endswith("#feedback")]
    expected = (KO_PAGE if lang == "ko" else EN_PAGE) + "#feedback"
    assert found == [expected]


@pytest.mark.parametrize("name, lang", READMES.items())
def test_readme_tool_page_link_per_language(name, lang):
    found = [h for h in links(read(name)) if "utm_source=github" in h and "chaekgalpi.co.kr" in h]
    assert len(found) == 1
    assert page_of(found[0]) == (KO_PAGE if lang == "ko" else EN_PAGE)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(found[0]).query)
    assert q == {"utm_source": ["github"], "utm_medium": ["referral"], "utm_campaign": ["tool_cta"],
                 "utm_content": ["photoorganizer"]}  # the tail is unchanged


@pytest.mark.parametrize("name, note", [("README.ja.md", "（韓国語のみ）"), ("README.zh-CN.md", "（仅韩语）")])
def test_korean_only_notes_stay(name, note):
    """The reading-log service is in Korean only - still true, so the note stays (ⓓ)."""
    assert note in read(name)


def doc_files():
    """The READMEs and every release note (they go on GitHub's release page as they are)."""
    folder = os.path.join(ROOT, "docs", "releases")
    return list(READMES) + sorted(f"docs/releases/{n}" for n in os.listdir(folder) if n.endswith(".md"))


def test_this_version_has_release_notes():
    assert f"docs/releases/v{APP_VERSION}.md" in doc_files()


@pytest.mark.parametrize("name", doc_files())
def test_no_link_swallows_a_bracket_or_the_next_word(name):
    """GitHub turns a bare address into a link up to the next space, so ")" or 의 / ，。 right
    after it become part of the address (global rule 11). Links are [text](address)."""
    text = read(name)
    hrefs = links(text)
    assert hrefs or name not in READMES
    for href in hrefs:
        assert href.startswith(("https://", "http://", "#", "docs/", "LICENSE", "README")) or "." in href, href
        assert not BAD_TAIL.search(href), href
    bare = BARE_URL.findall(MD_LINK.sub("", text))
    for url in bare:
        assert not BAD_TAIL.search(url), f"{name}: bare address runs into the text: {url}"


def test_the_link_checker_catches_the_broken_forms():
    for broken in ("의견 칸(https://chaekgalpi.co.kr/tools/x#feedback)이", "https://chaekgalpi.co.kr/x，下一句",
                   "https://chaekgalpi.co.kr/x에서", "https://chaekgalpi.co.kr/x。"):
        assert any(BAD_TAIL.search(u) for u in BARE_URL.findall(MD_LINK.sub("", broken))), broken
    assert not any(BAD_TAIL.search(u) for u in BARE_URL.findall(MD_LINK.sub("", "[칸](https://a.b/c#d)이")))


# ====================================================================== the window
@pytest.fixture(scope="module")
def root():
    r = gui.TkinterDnD.Tk() if gui._HAS_DND else tk.Tk()
    r.geometry(f"{theme.WINDOW[0]}x{theme.WINDOW[1]}+10+10")
    r.minsize(*theme.WINDOW_MIN)
    yield r
    r.destroy()


@pytest.fixture
def app(root, monkeypatch, no_sockets):
    i18n.set_lang("en", persist=False)
    for child in root.winfo_children():
        child.destroy()
    root.geometry(f"{theme.WINDOW[0]}x{theme.WINDOW[1]}+10+10")
    a = gui.App(root)
    a.opened = []
    a.open_result = True

    def fake_open(url, *args, **kwargs):
        a.opened.append(url)
        return a.open_result

    monkeypatch.setattr(gui.webbrowser, "open", fake_open)
    root.update()
    yield a
    a.tip_feedback.hide()
    if a.feedback_win is not None and a.feedback_win.winfo_exists():
        a.feedback_win.destroy()
    a.cancel.set()
    if a.replan_job is not None:
        root.after_cancel(a.replan_job)
    a._poll = lambda: None
    assert no_sockets == []  # f. nothing left the machine while the window was up


def pump(root, cond, timeout=3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.01)
    return False


def press(widget, sequence: str) -> str:
    """Run what Tk runs when `sequence` reaches `widget`: each bind tag's script in order
    (widget, class, window, all) with %-fields filled in, until one returns break."""
    for tag in widget.bindtags():
        script = widget.bind(sequence) if tag == str(widget) else widget.bind_class(tag, sequence)
        if not script:
            continue
        fields = dict.fromkeys("#bhkstwxyEXYDN", "0")
        fields.update(W=str(widget), K="??", A="{}")
        script = re.sub(r"%([#a-zA-Z])", lambda m: fields.get(m.group(1), "0"), script)
        code = widget.tk.eval(f"catch {{{script}}} ::press_result")
        assert code in ("0", "3"), widget.tk.eval("set ::press_result")  # 3 is Tcl's break
        if code == "3":
            widget.update()
            return "break"
    widget.update()
    return ""


def switch(app, lang):
    app.lang_box.set(i18n.LANG_NAMES[lang])
    app._on_lang()
    app.root.update()


def url_for(lang):
    return uc.feedback_url(SLUG, APP_VERSION, lang)


# ---------------------------------------------------------------------- a. keyboard
def test_the_button_is_a_tab_stop(app):
    btn = app.feedback_btn
    assert btn.winfo_class() == "TButton"
    assert app.root.tk.call("tk::FocusOK", btn) == 1
    assert str(btn.cget("takefocus")) in ("", "1", "ttk::takefocus")


def test_tab_order_reads_left_to_right_across_the_header(app):
    btn = app.feedback_btn
    assert btn.tk_focusNext() is app.lang_box  # Tab from the button: the language box
    assert app.lang_box.tk_focusNext() is app.gear_btn  # then the gear
    assert app.lang_box.tk_focusPrev() is btn  # Shift+Tab back onto the button
    before = btn.tk_focusPrev()
    assert before is not btn and before.tk_focusNext() is btn  # the widget before it: Tab lands on it
    assert app.gear_btn.tk_focusPrev() is app.lang_box


def test_a_full_tab_cycle_visits_the_button_once(app):
    w, seen = app.feedback_btn, []
    for _ in range(200):
        w = w.tk_focusNext()
        seen.append(w)
        if w is app.feedback_btn:
            break
    assert seen[-1] is app.feedback_btn and seen.count(app.feedback_btn) == 1
    back, w = [], app.feedback_btn
    for _ in range(len(seen)):
        w = w.tk_focusPrev()
        back.append(w)
    assert back == list(reversed(seen[:-1])) + [app.feedback_btn]  # Shift+Tab walks it backwards


def test_tab_is_tk_s_own_traversal(app):
    btn = app.feedback_btn
    assert "tk_focusNext" in btn.bind_class("all", "<<NextWindow>>")
    assert "tk_focusPrev" in btn.bind_class("all", "<<PrevWindow>>")
    for seq in ("<Tab>", "<Shift-Tab>", "<<NextWindow>>", "<<PrevWindow>>"):
        assert not btn.bind(seq)  # the button does not take Tab over


@pytest.mark.parametrize("key", ["<Return>", "<KP_Enter>", "<space>"])
def test_enter_and_space_open_the_page_exactly_once(app, key):
    assert press(app.feedback_btn, key) == "break"
    pump(app.root, lambda: False, 0.3)  # a class binding would invoke after idle: give it time
    assert app.opened == [url_for("en")]
    assert app.feedback_win is None


def test_space_s_class_binding_would_have_opened_it_too(app):
    """Why "break" matters: without it the TButton class binding of Space invokes again."""
    assert "ttk::button::activate" in app.feedback_btn.bind_class("TButton", "<space>")


def test_each_key_and_the_mouse_open_one_page_each(app):
    btn = app.feedback_btn
    for key in ("<Return>", "<space>", "<KP_Enter>"):
        press(btn, key)
    btn.event_generate("<Enter>", x=3, y=3)
    btn.event_generate("<ButtonPress-1>", x=3, y=3)
    btn.event_generate("<ButtonRelease-1>", x=3, y=3)
    pump(app.root, lambda: False, 0.3)
    assert app.opened == [url_for("en")] * 4


@pytest.mark.parametrize("lang", i18n.LANGS)
def test_keyboard_follows_the_current_language(app, lang):
    switch(app, lang)
    press(app.feedback_btn, "<Return>")
    assert app.opened == [url_for(lang)]
    assert page_of(app.opened[0]) == (KO_PAGE if lang == "ko" else EN_PAGE)


def test_keyboard_with_no_browser_shows_the_address_window(app):
    app.open_result = False
    press(app.feedback_btn, "<space>")
    assert app.opened == [url_for("en")]
    assert app.feedback_win is not None and app.feedback_entry.get() == url_for("en")


@pytest.mark.parametrize("lang", i18n.LANGS)
def test_focus_frame_on_and_off_without_changing_size(app, lang):
    switch(app, lang)
    btn = app.feedback_btn
    size = (btn.winfo_reqwidth(), btn.winfo_reqheight())
    geometry = (btn.winfo_x(), btn.winfo_width(), app.lang_box.winfo_x())
    assert not btn.instate(["focus"])
    btn.event_generate("<FocusIn>", detail="NotifyAncestor")
    app.root.update()
    assert btn.instate(["focus"])  # what the theme draws its focus frame for
    assert (btn.winfo_reqwidth(), btn.winfo_reqheight()) == size
    assert (btn.winfo_x(), btn.winfo_width(), app.lang_box.winfo_x()) == geometry  # nothing moved
    btn.event_generate("<FocusOut>", detail="NotifyAncestor")
    app.root.update()
    assert not btn.instate(["focus"])
    assert (btn.winfo_reqwidth(), btn.winfo_reqheight()) == size
    assert app.opened == []  # focus alone opens nothing


def test_the_theme_draws_a_frame_for_a_focused_toolbutton(app):
    """sv-ttk gives Toolbutton.button its own image in the focus state (button-focus);
    the capture in the report shows it. Checked in the theme files the program ships."""
    import sv_ttk

    assert app.feedback_btn.cget("style") == "Toolbutton"
    assert ttk_theme(app).startswith("sun-valley")
    folder = os.path.join(os.path.dirname(sv_ttk.__file__), "theme")
    for name in ("light.tcl", "dark.tcl"):
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            text = f.read()
        block = text[text.index("element create Toolbutton.button"):]
        block = block[:block.index("]")]
        assert re.search(r"\bfocus \$I\(button-focus\)", block), name


def ttk_theme(app) -> str:
    return app.root.tk.call("ttk::style", "theme", "use")


@pytest.mark.parametrize("seq", ["<Key-a>", "<Key-A>", "<Escape>", "<BackSpace>", "<Delete>", "<Up>", "<Down>",
                                 "<Left>", "<Right>", "<F1>", "<Key-1>", "<Control-Key-c>", "<Alt-Key-f>"])
def test_other_keys_open_nothing(app, seq):
    press(app.feedback_btn, seq)
    pump(app.root, lambda: False, 0.15)
    assert app.opened == [] and app.feedback_win is None
    assert not app.feedback_btn.bind(seq)


def test_only_enter_and_space_are_bound_on_the_button(app):
    keys = {s for s in app.feedback_btn.bind() if "Key" in s or s in ("<Return>", "<space>")}
    assert keys == {"<Key-Return>", "<Key-KP_Enter>", "<Key-space>"}


# ---------------------------------------------------------------------- b. the note
def hover(app, timeout=3.0) -> bool:
    app.feedback_btn.event_generate("<Enter>", x=3, y=3)
    return pump(app.root, lambda: app.tip_feedback.tip is not None and app.tip_feedback.tip.winfo_ismapped(),
                timeout)


def test_delay_is_about_half_a_second():
    assert 400 <= gui.Tooltip.DELAY_MS <= 500


def test_the_note_appears_on_hover_and_goes_on_leave(app):
    tip = app.tip_feedback
    assert tip.tip is None
    start = time.time()
    assert hover(app)
    assert time.time() - start >= gui.Tooltip.DELAY_MS / 1000 - 0.05  # not at once
    assert tip.label.cget("text") == TIPS["en"]
    app.feedback_btn.event_generate("<Leave>", x=3, y=3)
    app.root.update()
    assert tip.tip is None
    assert hover(app)  # and again
    assert app.opened == []  # hovering opens nothing


def test_leaving_before_the_delay_shows_nothing(app):
    app.feedback_btn.event_generate("<Enter>", x=3, y=3)
    pump(app.root, lambda: False, 0.15)
    app.feedback_btn.event_generate("<Leave>", x=3, y=3)
    pump(app.root, lambda: False, 0.7)
    assert app.tip_feedback.tip is None


def test_a_click_hides_the_note_and_opens_once(app):
    assert hover(app)
    btn = app.feedback_btn
    btn.event_generate("<ButtonPress-1>", x=3, y=3)
    app.root.update()
    assert app.tip_feedback.tip is None
    btn.event_generate("<ButtonRelease-1>", x=3, y=3)
    pump(app.root, lambda: False, 0.7)
    assert app.tip_feedback.tip is None and app.opened == [url_for("en")]


@pytest.mark.parametrize("key", ["<Return>", "<space>"])
def test_a_key_press_hides_the_note_too(app, key):
    assert hover(app)
    press(app.feedback_btn, key)
    assert app.tip_feedback.tip is None and app.opened == [url_for("en")]


def test_the_note_is_a_small_borderless_window_under_the_button(app):
    assert hover(app)
    tip, btn = app.tip_feedback.tip, app.feedback_btn
    assert tip.wm_overrideredirect() and tip.winfo_toplevel() is tip
    assert tip.winfo_rooty() >= btn.winfo_rooty() + btn.winfo_height()
    assert tip.winfo_width() < app.root.winfo_width()


@pytest.mark.parametrize("lang", i18n.LANGS)
def test_note_words_per_language(app, lang):
    switch(app, lang)
    assert app.tip_feedback.text == TIPS[lang]
    assert hover(app)
    assert app.tip_feedback.label.cget("text") == TIPS[lang]


def test_language_files_carry_four_distinct_notes():
    notes = {}
    for lang in i18n.LANGS:
        with open(os.path.join(ROOT, "lang", f"{lang}.json"), encoding="utf-8") as f:
            notes[lang] = json.load(f)["tip_feedback"]
    assert notes == TIPS
    assert all(n.strip() for n in notes.values()) and len(set(notes.values())) == len(notes)


def test_switching_language_while_the_note_shows_changes_its_words(app):
    assert hover(app)
    for lang in ("ko", "ja", "zh-CN", "en"):
        switch(app, lang)
        tip = app.tip_feedback
        assert tip.tip is not None and tip.tip.winfo_ismapped()
        assert tip.label.cget("text") == TIPS[lang]
        assert tip.widget is app.feedback_btn  # it follows the rebuilt button
    app.feedback_btn.event_generate("<Leave>", x=3, y=3)
    app.root.update()
    assert app.tip_feedback.tip is None


def test_a_theme_switch_keeps_the_note_with_the_new_colours(app, monkeypatch):
    assert hover(app)
    monkeypatch.setattr(gui.prefs_mod, "save", lambda p: None)
    app.prefs.theme = "dark"
    app.settings_changed("theme")
    app.root.update()
    assert app.tip_feedback.tip is not None
    assert app.tip_feedback.label.cget("bg").lower() == theme.DARK["surface"].lower()
    app.prefs.theme = "light"
    app.settings_changed("theme")
    app.root.update()
    assert app.tip_feedback.label.cget("bg").lower() == theme.LIGHT["surface"].lower()


def test_the_note_goes_when_its_button_goes_for_good(app):
    assert hover(app)
    tip = app.tip_feedback.tip
    app.feedback_btn.destroy()
    pump(app.root, lambda: app.tip_feedback.tip is None, 1.0)
    assert app.tip_feedback.tip is None and not tip.winfo_exists()


def inside(rect, area) -> bool:
    x0, y0, x1, y1 = rect
    return area[0] <= x0 and x1 <= area[2] and area[1] <= y0 and y1 <= area[3]


@pytest.mark.parametrize("lang", i18n.LANGS)
@pytest.mark.parametrize("edge", ["left", "right", "past_left", "past_right"])
def test_note_stays_in_the_window_and_on_the_screen_at_the_minimum_size(app, lang, edge):
    root = app.root
    switch(app, lang)
    w, h = theme.px(theme.WINDOW_MIN[0]), theme.px(theme.WINDOW_MIN[1])
    area = gui.screen_area(root)
    x = {"left": area[0], "right": area[2] - w - 16, "past_left": area[0] - 200, "past_right": area[2] - w + 200}[edge]
    root.geometry(f"{w}x{h}+{x}+{area[1] + 20}")
    pump(root, lambda: False, 0.3)
    assert hover(app)
    tip = app.tip_feedback.tip
    rect = (tip.winfo_rootx(), tip.winfo_rooty(), tip.winfo_rootx() + tip.winfo_width(),
            tip.winfo_rooty() + tip.winfo_height())
    area = gui.screen_area(app.feedback_btn)
    assert inside(rect, area), (rect, area)  # never cut by the screen edge
    win_left, win_right = root.winfo_rootx(), root.winfo_rootx() + root.winfo_width()
    if edge in ("left", "right"):  # the window is fully on screen: the note stays within it
        assert win_left <= rect[0] and rect[2] <= win_right, (rect, win_left, win_right)
    assert app.tip_feedback.label.cget("text") == TIPS[lang]
    root.geometry(f"{theme.WINDOW[0]}x{theme.WINDOW[1]}+10+10")


# the placement rule on its own: (x, y, w, h) of the button, (w, h) of the note, the monitor
AREA = (0, 0, 1920, 1040)


def test_placement_under_the_button_right_edges_lined_up():
    assert gui.tip_position((1500, 100, 100, 30), (300, 40), AREA, (900, 1700)) == (1300, 130 + gui.px(4))


def test_placement_kept_inside_the_window():
    x, _ = gui.tip_position((910, 100, 100, 30), (300, 40), AREA, (900, 1700))
    assert x == 900


def test_placement_wider_than_the_window_falls_back_to_the_screen():
    x, _ = gui.tip_position((10, 100, 100, 30), (500, 40), AREA, (0, 400))
    assert x == 0


@pytest.mark.parametrize("button_x, expected", [(1880, 1920 - 300), (-50, 0)])
def test_placement_never_off_the_monitor_sideways(button_x, expected):
    x, _ = gui.tip_position((button_x, 100, 100, 30), (300, 40), AREA)
    assert x == min(max(button_x + 100 - 300, 0), 1920 - 300) == expected


def test_placement_above_when_there_is_no_room_below():
    x, y = gui.tip_position((1500, 1020, 100, 30), (300, 40), AREA)
    assert y == 1020 - gui.px(4) - 40


def test_placement_on_a_monitor_left_of_the_main_one():
    area = (-1920, 0, 0, 1040)
    x, y = gui.tip_position((-1950, 100, 100, 30), (300, 40), area)
    assert -1920 <= x and x + 300 <= 0


# ====================================================================== f. no network
def test_rule_readmes_and_note_touch_no_network(no_sockets):
    for lang in KOREAN + NOT_KOREAN + MISSING:
        uc.feedback_url(SLUG, APP_VERSION, lang)
        uc.page_url(info(), lang)
    assert no_sockets == []


def test_the_network_guard_is_on_for_every_test(no_sockets):
    import socket

    with pytest.raises(OSError):
        socket.create_connection(("chaekgalpi.co.kr", 443), timeout=1)
    with pytest.raises(OSError):
        socket.getaddrinfo("example.com", 80)
    assert len(no_sockets) == 2
    no_sockets.clear()  # the guard did its job; this test must not fail its own teardown
    import conftest

    del conftest.NETWORK_ATTEMPTS[-2:]
