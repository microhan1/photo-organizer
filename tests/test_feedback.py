""""Send feedback" (v0.1.5): the address it opens, the strings it shows, and that the CLI
is unchanged. The menu itself is tested in test_gui.py. Nothing here opens a browser or
reaches the network."""
from __future__ import annotations

import json
import os
import urllib.parse

import pytest
from conftest import ROOT
from test_cases import run_cli

import i18n
import update_check as uc
from version import APP_VERSION

SLUG = "photoorganizer"
KO = "https://chaekgalpi.co.kr/tools/photoorganizer?utm_source=app&utm_medium=feedback&v={v}#feedback"
EN = "https://chaekgalpi.co.kr/en/tools/photoorganizer?utm_source=app&utm_medium=feedback&v={v}#feedback"


def parts(url: str):
    p = urllib.parse.urlsplit(url)
    return p, urllib.parse.parse_qs(p.query, keep_blank_values=True)


# ------------------------------------------------------------------ a. the address per language
@pytest.mark.parametrize("lang, expected", [("ko", KO), ("en", EN), ("ja", EN), ("zh-CN", EN)])
def test_the_exact_address_for_each_language(lang, expected):
    assert uc.feedback_url(SLUG, "0.2.1", lang) == expected.format(v="0.2.1")


@pytest.mark.parametrize("lang", [None, "", "  ", "fr", "de", "xx", "zh-TW", "english"])
def test_unknown_or_missing_language_gets_the_english_page(lang):
    """Series rule of 2026-10-27: only ko... is Korean; no language is the default (en).
    Until v0.1.5 these got the Korean page (test_feedback_access.py has the full rule)."""
    assert uc.feedback_url(SLUG, "0.2.1", lang) == EN.format(v="0.2.1")


def test_korean_region_codes_get_the_korean_page():
    assert uc.feedback_url(SLUG, "0.2.1", "ko-KR") == KO.format(v="0.2.1")


@pytest.mark.parametrize("lang", ["EN", "En", " en ", "JA", "zh-cn", "ZH-CN", "zh_CN", "KO", "Ko"])
def test_language_codes_ignore_case_and_spacing(lang):
    expected = KO if lang.strip().lower() == "ko" else EN
    assert uc.feedback_url(SLUG, "0.2.1", lang) == expected.format(v="0.2.1")


@pytest.mark.parametrize("lang", ["ko", "en", "ja", "zh-CN", None])
@pytest.mark.parametrize("version", ["0.2.1", "", None, "a b&c#d"])
def test_every_address_is_on_the_site_ends_at_the_box_and_is_counted_as_the_app(lang, version):
    url = uc.feedback_url(SLUG, version, lang)
    assert url.startswith("https://chaekgalpi.co.kr/") and url.startswith(uc.SITE)
    assert url.endswith("#feedback") and url.count("#") == 1
    p, q = parts(url)
    assert p.scheme == "https" and p.netloc == "chaekgalpi.co.kr" and p.fragment == "feedback"
    assert p.path in ("/tools/photoorganizer", "/en/tools/photoorganizer")
    assert q["utm_source"] == ["app"] and q["utm_medium"] == ["feedback"]
    assert "utm_source=app" in url and "utm_medium=feedback" in url


def test_the_program_uses_its_own_version():
    assert uc.feedback_url(SLUG, APP_VERSION, "ko") == KO.format(v=APP_VERSION)
    assert APP_VERSION and urllib.parse.quote(APP_VERSION, safe="") == APP_VERSION  # a plain x.y.z


# ------------------------------------------------------------------ b. versions
@pytest.mark.parametrize("version", ["0.2.1", "v0.2.1", "1.0.0-beta+3", "10.20.30", "0.1.5.dev0"])
def test_ordinary_versions_come_back_unchanged(version):
    for lang in ("ko", "en"):
        _, q = parts(uc.feedback_url(SLUG, version, lang))
        assert q["v"] == [version]


def test_plus_is_encoded_so_it_does_not_turn_into_a_space():
    url = uc.feedback_url(SLUG, "1.0.0-beta+3", "ko")
    assert "v=1.0.0-beta%2B3#feedback" in url


@pytest.mark.parametrize("version", ["", None, " ", "\t"])
def test_no_version_leaves_v_out(version):
    url = uc.feedback_url(SLUG, version, "ko")
    assert url == "https://chaekgalpi.co.kr/tools/photoorganizer?utm_source=app&utm_medium=feedback#feedback"
    assert "v" not in parts(url)[1]


@pytest.mark.parametrize("version", ["0.2 beta", "a&utm_source=evil", "1#2", "버전 1.0", "1/2?x=y", "%41",
                                     " 0.2.1 ", "v=3&v=4", "版本 一", "　全角"])
def test_odd_versions_are_encoded_and_cannot_break_the_address(version):
    for lang in ("ko", "en", "ja", "zh-CN"):
        url = uc.feedback_url(SLUG, version, lang)
        p, q = parts(url)
        assert q["v"] == [version]  # read back exactly
        assert q["utm_source"] == ["app"] and q["utm_medium"] == ["feedback"] and len(q) == 3
        assert p.fragment == "feedback" and url.count("#") == 1 and url.endswith("#feedback")
        assert p.netloc == "chaekgalpi.co.kr"
        assert all(32 < ord(ch) < 127 for ch in url)  # spaces, Hangul and the like are all %-encoded


def test_the_address_is_built_from_constants_only():
    """No setting or server answer decides where the button goes."""
    assert uc.FEEDBACK.startswith(uc.SITE) and uc.FEEDBACK_EN.startswith(uc.SITE)
    import inspect

    params = list(inspect.signature(uc.feedback_url).parameters)
    assert params == ["slug", "version", "lang"]


# ------------------------------------------------------------------ e. strings
WORDING = {
    "ko": "의견 보내기",
    "en": "Send feedback",
    "ja": "ご意見を送る",
    "zh-CN": "发送反馈",
}
NEW_KEYS = ["btn_feedback", "feedback_failed", "btn_copy", "btn_copied", "btn_close"]


def load(code):
    with open(os.path.join(ROOT, "lang", f"{code}.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("lang", i18n.LANGS)
def test_new_strings_exist_and_are_not_empty_in_every_language(lang):
    data = load(lang)
    for key in NEW_KEYS:
        assert isinstance(data.get(key), str) and data[key].strip(), (lang, key)
    assert data["btn_feedback"] == WORDING[lang]
    assert data["btn_copy"] != data["btn_copied"]


def test_every_language_is_covered():
    assert set(i18n.LANGS) == set(WORDING)


# ------------------------------------------------------------------ f. the command line is unchanged
def test_cli_help_still_works_and_has_no_feedback_option(tmp_path):
    out = run_cli(tmp_path, "--help")
    assert out.returncode == 0 and "Traceback" not in out.stderr
    for opt in ("--pattern", "--dest", "--copy", "--dry-run", "--undo", "--lang", "--gui"):
        assert opt in out.stdout
    assert "feedback" not in out.stdout.lower()


def test_cli_version_is_still_not_an_option(tmp_path):
    """There never was a --version; the menu did not add one (it is rejected as before)."""
    out = run_cli(tmp_path, "--version")
    assert out.returncode == 2 and "Traceback" not in out.stderr
