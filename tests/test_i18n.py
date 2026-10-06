"""Language files: same keys and placeholders everywhere, every key the code asks for exists,
and no user-facing text is written in the code itself."""
from __future__ import annotations

import ast
import json
import os
import re

import pytest
from conftest import ROOT

import i18n

PRD_KEYS = ["app_title", "drop_hint", "drop_sub", "scan_summary", "opt_pattern", "opt_convert", "opt_dedupe",
            "opt_similar", "opt_dest", "opt_use_mtime", "col_thumb", "col_current", "col_date", "col_basis", "col_new",
            "col_status", "basis_exif", "basis_video", "basis_name", "basis_pair", "basis_mtime", "status_ok",
            "status_convert", "status_nodate", "status_dup", "status_conflict", "status_same", "folder_nodate",
            "folder_dupes", "folder_heic", "summary", "btn_run", "btn_stop", "btn_undo", "btn_open", "btn_details",
            "msg_scanning", "msg_done", "msg_failed", "msg_similar_estimate", "msg_cloud_warning", "msg_no_log",
            "err_log_write"]
# keys built at run time from a prefix and a known value
DYNAMIC = {
    "basis_": ["exif", "exif_suspect", "video", "name", "name_multi", "pair", "mtime", "manual"],
    "status_": ["ok", "convert", "nodate", "dup", "conflict", "same", "similar"],
    "note_": ["empty", "burst", "meta_error", "similar", "suggest_dup", "tags_dropped"],
    "source_": ["kakao", "screenshot", "messenger", "camera", "other"],
    "sum_": ["move", "convert", "nodate", "dupes", "same", "folders"],
    "col_": ["current", "date", "basis", "new", "status", "thumb"],
    "view_": ["all", "todo", "nodate", "dup", "failed"],
    "theme_": ["light", "dark", "system"], "mode_": ["move", "copy", "organize", "heic_only"],
    "dupes_": ["move", "trash"], "heic_": ["keep", "move"], "icc_": ["keep", "srgb"],
    "err_pattern_": ["empty", "unknown", "braces", "absolute"],
}
CODE = ["main.py", "gui.py", "mover.py", "undo.py", "plan.py", "prefs.py", "scan.py", "dates.py", "convert.py",
        "dedupe.py", "theme.py", "i18n.py", "longpath.py", "patterns.py"]
# modules whose non-ASCII literals are not UI text: font family names, filename regexes, language names
NOT_UI = {"theme.py": "font names", "patterns.py": "filename patterns", "dedupe.py": "copy-name pattern",
          "i18n.py": "language names"}


def load(code):
    with open(os.path.join(ROOT, "lang", f"{code}.json"), encoding="utf-8") as f:
        return json.load(f)


def test_same_keys_and_placeholders_in_every_language():
    en = load("en")
    for code in i18n.LANGS:
        data = load(code)
        assert set(data) == set(en), code
        for key, text in data.items():
            assert set(re.findall(r"\{(\w+)", text)) == set(re.findall(r"\{(\w+)", en[key])), (code, key)


def test_prd_keys_exist():
    assert [k for k in PRD_KEYS if k not in load("en")] == []


def test_every_key_the_code_uses_exists():
    en = load("en")
    used = set()
    for name in CODE:
        src = open(os.path.join(ROOT, name), encoding="utf-8").read()
        used |= set(re.findall(r"""\bt\(\s*["']([a-z0-9_]+)["']""", src))
        used |= set(re.findall(r"""["'](err_[a-z_]+|msg_[a-z_]+|note_[a-z_]+)["']""", src))
    for prefix, values in DYNAMIC.items():
        used |= {prefix + v for v in values}
    import theme

    used -= set(theme.LIGHT)  # colour names such as "err_band" are not text keys
    missing = sorted(k for k in used if k not in en)
    assert missing == []


def test_prd_folder_names():
    assert {load(c)["folder_nodate"] for c in i18n.LANGS} == {"날짜 없음", "No Date", "无日期", "日付なし"}
    assert load("ko")["folder_dupes"] == "_중복" and load("en")["folder_dupes"] == "_Duplicates"
    assert load("ko")["folder_heic"] == "_원본HEIC" and load("en")["folder_heic"] == "_Original_HEIC"


def test_prd_wording_ko_en():
    ko, en = load("ko"), load("en")
    assert ko["msg_done"] == "완료: {move}개 이동, {convert}개 변환, 로그 저장됨"
    assert en["msg_cloud_warning"] == "Skipping {count} cloud-only files (download them first)"
    assert ko["summary"] == "이동 {move} · 변환 {convert} · 날짜 없음 {nodate} · 중복 {dupes} · 변경 없음 {same}"


@pytest.mark.parametrize("name", CODE)
def test_no_user_text_written_in_code(name):
    if name in NOT_UI:
        return
    tree = ast.parse(open(os.path.join(ROOT, name), encoding="utf-8").read())
    bad = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
           and any(ord(ch) > 127 for ch in n.value) and not _is_docstring(n, tree)]
    assert bad == []


def _is_docstring(node, tree) -> bool:
    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and body[0].value is node:
            return True
    return False


def test_switching_language():
    i18n.set_lang("ko", persist=False)
    assert i18n.t("btn_run") == "실행"
    i18n.set_lang("ja", persist=False)
    assert i18n.t("summary", move=1, convert=2, nodate=3, dupes=4, same=5).startswith("移動 1")
    i18n.set_lang("xx", persist=False)
    assert i18n.current_lang() == "en"


def test_os_language_picks_one_of_four_or_english(monkeypatch):
    assert i18n.detect_os_lang() in i18n.LANGS


def test_iso_dates_in_folder_names_in_every_language():
    import datetime

    import plan as plan_mod

    for code in i18n.LANGS:
        i18n.set_lang(code, persist=False)
        assert plan_mod.render_pattern(plan_mod.DEFAULT_PATTERN, datetime.datetime(2024, 3, 15), "kakao", ".jpg") == \
            ["2024", "2024-03-15"]


def test_source_folder_names_follow_the_language():
    import datetime

    import plan as plan_mod

    i18n.set_lang("ko", persist=False)
    assert plan_mod.render_pattern("{yyyy}/{source}/{yyyy-mm-dd}", datetime.datetime(2024, 3, 15), "kakao", ".jpg") \
        == ["2024", "카카오톡", "2024-03-15"]
