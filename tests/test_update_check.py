"""Update check: once a day, off switch, silent failures, what is sent, when a notice shows.
The network is always faked - these tests never reach the real server."""
from __future__ import annotations

import io
import json
import os
import socket
import sys
import urllib.error
import urllib.parse

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import prefs as prefs_mod  # noqa: E402
import update_check as uc  # noqa: E402
from version import APP_VERSION  # noqa: E402

DAY = "2026-10-13"
SLUG = "photoorganizer"
# the real server's shape (probed 2026-10-13), with a newer version
OK = {"slug": SLUG, "name": "사진 정리", "latest": "0.2.0", "tag": "v0.2.0", "client": APP_VERSION,
      "updateAvailable": True,
      "url": "https://chaekgalpi.co.kr/tools/photoorganizer?utm_source=app&utm_medium=update&utm_campaign=tool_update"
             "&utm_content=photoorganizer",
      "downloadUrl": "https://chaekgalpi.co.kr/tools/photoorganizer/download", "source": "github"}


class FakeRoot:
    """after() runs the callback at once; the background thread is run inline (see fixture)."""

    def __init__(self):
        self.delays = []

    def after(self, ms, fn):
        self.delays.append(ms)
        fn()


@pytest.fixture(autouse=True)
def inline_threads(monkeypatch):
    class InlineThread:
        def __init__(self, target, name=None, daemon=None):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(uc.threading, "Thread", InlineThread)


def run(prefs, fetcher, day=DAY):
    """One app start: schedule the check; the caller records the day only when answered (as the GUI does)."""
    results = []

    def on_result(info, answered):
        results.append(info)
        if answered:
            prefs.update_check_last = day

    root = FakeRoot()
    scheduled = uc.check_async(root, SLUG, APP_VERSION, prefs, on_result, fetcher=fetcher, day=day)
    return scheduled, results, root


def counting(answer):
    calls = []

    def fetcher(slug, version):
        calls.append((slug, version))
        return answer

    return fetcher, calls


def test_tests_never_reach_the_real_server():
    """conftest replaces the defaults the program uses (fetcher, opener) for every test:
    a bare fetch() must be refused and recorded, never sent (LESSONS D18)."""
    import conftest
    import urllib.request

    assert uc.check_async.__kwdefaults__["fetcher"] is conftest._no_fetch
    assert uc.fetch.__kwdefaults__["opener"] is conftest._no_network
    assert urllib.request.urlopen is conftest._no_network
    assert uc.fetch(SLUG, APP_VERSION) is None
    assert len(conftest._blocked) == 1 and "chaekgalpi.co.kr" in conftest._blocked[0]
    conftest._blocked.clear()  # the guard did its job; this test must not fail its own teardown


def test_version_is_a_release_number():
    assert APP_VERSION.count(".") == 2 and all(p.isdigit() for p in APP_VERSION.split("."))


# ------------------------------------------------------------------ how often
def test_same_day_twice_asks_once():
    p = prefs_mod.Prefs()
    fetcher, calls = counting(OK)
    run(p, fetcher)
    run(p, fetcher)
    assert len(calls) == 1
    run(p, fetcher, day="2026-10-14")
    assert len(calls) == 2


def test_switched_off_never_asks():
    p = prefs_mod.Prefs(update_check=False)
    fetcher, calls = counting(OK)
    scheduled, results, root = run(p, fetcher)
    assert not scheduled and calls == [] and results == []


def test_on_by_default():
    assert prefs_mod.Prefs().update_check is True


def test_offline_does_not_use_up_the_day():
    p = prefs_mod.Prefs()
    fetcher, calls = counting(None)  # no answer
    run(p, fetcher)
    run(p, fetcher)
    assert len(calls) == 2 and p.update_check_last == ""


def test_waits_one_and_a_half_seconds_first():
    _, _, root = run(prefs_mod.Prefs(), counting(OK)[0])
    assert root.delays[0] == 1500


def test_sends_this_tools_slug_and_version():
    fetcher, calls = counting(OK)
    run(prefs_mod.Prefs(), fetcher)
    assert calls == [(SLUG, APP_VERSION)]


# ------------------------------------------------------------------ what is sent
class FakeResp(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_request_carries_only_slug_version_and_os():
    seen = {}

    def opener(req, timeout):
        seen["url"], seen["timeout"] = req.full_url, timeout
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return FakeResp(json.dumps(OK).encode())

    assert uc.fetch(SLUG, APP_VERSION, opener=opener) == OK
    url = urllib.parse.urlsplit(seen["url"])
    assert (url.scheme, url.netloc, url.path) == ("https", "chaekgalpi.co.kr", "/api/tools/photoorganizer/latest")
    assert urllib.parse.parse_qs(url.query) == {"v": [APP_VERSION], "os": ["win"]}
    assert seen["timeout"] == 4.0
    assert seen["headers"]["user-agent"] == f"chaekgalpi-tool/photoorganizer/{APP_VERSION}"
    assert set(seen["headers"]) <= {"user-agent", "accept"}  # nothing else about the user, photos or machine


@pytest.mark.parametrize("behaviour", ["urlerror", "timeout", "http500", "badjson", "list", "status500", "oserror"])
def test_failures_are_silent(behaviour):
    def opener(req, timeout):
        if behaviour == "urlerror":
            raise urllib.error.URLError("offline")
        if behaviour == "timeout":
            raise socket.timeout("slow")
        if behaviour == "http500":
            raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)
        if behaviour == "oserror":
            raise OSError("reset")
        if behaviour == "badjson":
            return FakeResp(b"<html>not json")
        if behaviour == "list":
            return FakeResp(b"[1, 2]")
        return FakeResp(json.dumps(OK).encode(), status=500)

    assert uc.fetch(SLUG, APP_VERSION, opener=opener) is None


def test_a_huge_answer_is_cut_off_not_read_whole():
    def opener(req, timeout):
        return FakeResp(b"{" + b" " * (2 * uc.MAX_BYTES) + b"}")

    assert uc.fetch(SLUG, APP_VERSION, opener=opener) is None  # cut mid-JSON: bad JSON, silent


# ------------------------------------------------------------------ when a notice shows
@pytest.mark.parametrize("flag", [False, None, "true", 1])
def test_no_notice_unless_server_says_true(flag):
    assert uc.interpret(dict(OK, updateAvailable=flag), APP_VERSION) is None


def test_the_real_servers_current_answer_shows_nothing():
    """What the server answered for the released version when this was written: not newer."""
    current = {"slug": SLUG, "latest": "0.1.2", "tag": "v0.1.2", "client": "0.1.2", "updateAvailable": False,
               "url": OK["url"], "source": "github"}
    assert uc.interpret(current, "0.1.2") is None


def test_notice_once_when_newer():
    p = prefs_mod.Prefs()
    fetcher, _ = counting(OK)
    _, results, _ = run(p, fetcher)
    assert len(results) == 1 and results[0] == uc.UpdateInfo("0.2.0", APP_VERSION, OK["url"])


def test_later_hides_that_version_but_not_a_newer_one():
    p = prefs_mod.Prefs(update_check_skipped="0.2.0")
    assert uc.interpret(OK, APP_VERSION, p.update_check_skipped) is None
    newer = dict(OK, latest="0.2.1")
    assert uc.interpret(newer, APP_VERSION, p.update_check_skipped).latest == "0.2.1"


def test_later_then_no_notice_on_the_next_start():
    p = prefs_mod.Prefs()
    fetcher, _ = counting(OK)
    _, results, _ = run(p, fetcher)
    p.update_check_skipped = results[0].latest  # [Later]
    _, results2, _ = run(p, fetcher, day="2026-10-14")
    assert results2 == [None]  # the server was asked (day recorded) but nothing is shown


@pytest.mark.parametrize("url", ["https://github.com/microhan1/photo-organizer/releases", "http://chaekgalpi.co.kr/tools/x",
                                 "https://chaekgalpi.co.kr.evil.example/", "javascript:alert(1)", None, 5])
def test_only_pages_of_the_tools_site_are_opened(url):
    assert uc.interpret(dict(OK, url=url), APP_VERSION) is None


EN_PAGE = "https://chaekgalpi.co.kr/en/tools/photoorganizer?utm_source=app&utm_medium=update"


def test_english_page_for_non_korean_screens_when_the_server_gives_one():
    info = uc.interpret(dict(OK, urlEn=EN_PAGE), APP_VERSION)
    assert info.url == OK["url"] and info.url_en == EN_PAGE
    assert uc.page_url(info, "ko") == OK["url"]
    for lang in ("en", "ja", "zh-CN"):
        assert uc.page_url(info, lang) == EN_PAGE
    # the request itself is unchanged: still only slug, version and os (test_request_carries_only...)


@pytest.mark.parametrize("url_en", [None, "", "https://example.com/en/tools/x", "http://chaekgalpi.co.kr/en/tools/x",
                                    "https://chaekgalpi.co.kr.evil.example/en", 7, "missing"])
def test_english_page_falls_back_to_the_korean_page(url_en):
    data = dict(OK) if url_en == "missing" else dict(OK, urlEn=url_en)
    info = uc.interpret(data, APP_VERSION)
    assert info is not None and info.url_en == ""
    for lang in ("ko", "en", "ja", "zh-CN"):
        assert uc.page_url(info, lang) == OK["url"]


@pytest.mark.parametrize("latest", ["", "   ", None, 3, "x" * 40])
def test_odd_versions_show_nothing(latest):
    assert uc.interpret(dict(OK, latest=latest), APP_VERSION) is None


def test_the_thread_holds_no_tk_object(monkeypatch):
    """The worker thread may only fetch into a box: if its closure held the root or the
    callback, a window closed mid-request would free Tk objects off the main thread
    (Tcl_AsyncDelete crash, scan-pdf-split)."""
    captured = {}

    class CaptureThread:
        def __init__(self, target, name=None, daemon=None):
            captured["target"] = target

        def start(self):
            pass

    monkeypatch.setattr(uc.threading, "Thread", CaptureThread)

    class Root:
        def after(self, ms, fn):
            if ms == uc.DELAY_MS:
                fn()

    root, on_result = Root(), lambda info, answered: None
    uc.check_async(root, SLUG, APP_VERSION, prefs_mod.Prefs(), on_result, fetcher=lambda s, v: OK, day=DAY)
    target = captured["target"]
    held = [c.cell_contents for c in (target.__closure__ or ())] + list(target.__defaults__ or ())
    assert not any(x is root or x is on_result for x in held)
    assert all(not callable(getattr(x, "after", None)) for x in held)


def test_closed_window_is_not_an_error():
    class Gone:
        def after(self, ms, fn):
            raise RuntimeError("main thread is not in main loop")

    assert uc.check_async(Gone(), SLUG, APP_VERSION, prefs_mod.Prefs(), lambda *a: None, fetcher=counting(OK)[0],
                          day=DAY) is False


# ------------------------------------------------------------------ settings
def test_settings_are_type_checked(monkeypatch):
    import i18n

    monkeypatch.setattr(i18n, "load_settings", lambda: {"update_check": "no", "update_check_last": 20261013,
                                                        "update_check_skipped": ["0.3.0"]})
    p = prefs_mod.load()
    assert p.update_check is True and p.update_check_last == "" and p.update_check_skipped == ""


def test_long_or_padded_settings_are_trimmed(monkeypatch):
    import i18n

    monkeypatch.setattr(i18n, "load_settings", lambda: {"update_check_last": " 2026-10-13T11:00 ",
                                                        "update_check_skipped": "0." + "9" * 60})
    p = prefs_mod.load()
    assert p.update_check_last == "2026-10-13" and len(p.update_check_skipped) == 32
