"""Dates in file names: the regex table (priority 4 of the date rules).

Each pattern has named groups y, m, d and optionally H, M, S. Specific
patterns (KakaoTalk, Samsung, screenshots...) come first and also tell where
the file came from; the general one finds a date anywhere in the name.
Users may add their own patterns in settings.json ("name_patterns"); those
run after the built-in specific ones and before the general one.
"""
from __future__ import annotations

import dataclasses
import datetime
import re

# where a file came from, for the {source} placeholder (names in lang/*.json: source_<key>)
KAKAO, SCREENSHOT, MESSENGER, CAMERA, OTHER = "kakao", "screenshot", "messenger", "camera", "other"
SOURCES = (KAKAO, SCREENSHOT, MESSENGER, CAMERA, OTHER)

_T = r"(?P<H>[01]\d|2[0-3])(?P<M>[0-5]\d)(?P<S>[0-5]\d)"
_D8 = r"(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})"

# (name, source, regex) -- matched against the file name without its extension
BUILTIN: tuple[tuple[str, str, str], ...] = (
    # KakaoTalk_20240315_123456789, KakaoTalk_Photo_2024-03-15-12-34-56, KakaoTalk_Video_...
    ("kakao", KAKAO,
     r"^KakaoTalk_(?:[A-Za-z]+_)?(?P<y>\d{4})-?(?P<m>\d{2})-?(?P<d>\d{2})[-_](?P<H>\d{2})-?(?P<M>\d{2})-?(?P<S>\d{2})"),
    # Screenshot_20240315_123456, Screenshot_2024-03-15-12-34-56, 스크린샷 2024-03-15 123456,
    # Screen Shot 2024-03-15 at 12.34.56, Screenshot 2024-03-15 123456
    ("screenshot", SCREENSHOT,
     r"^(?:Screenshot|Screen ?Shot|스크린샷|スクリーンショット|屏幕截图|截屏)[ _-]?"
     r"(?P<y>\d{4})-?(?P<m>\d{2})-?(?P<d>\d{2})(?:[ _-]|\s+at\s+)"
     r"(?P<H>\d{1,2})[.\-]?(?P<M>\d{2})[.\-]?(?P<S>\d{2})"),
    # Samsung camera: 20240315_123456, 20240315_123456(1)
    ("samsung", CAMERA, r"^" + _D8 + r"_" + _T + r"(?!\d)"),
    # Google Pixel / Android: PXL_20240315_123456789, IMG_20240315_123456, VID_..., MVIMG_..., PANO_...
    ("google", CAMERA, r"^(?:PXL|IMG|VID|MVIMG|PANO|BURST)_" + _D8 + r"_" + _T),
    # WhatsApp: IMG-20240315-WA0001 (date only)
    ("whatsapp", MESSENGER, r"^(?:IMG|VID|AUD|PTT|STK)-" + _D8 + r"-WA\d+"),
    # NAVER Band: band_20240315 (date only)
    ("band", MESSENGER, r"^band_" + _D8 + r"(?!\d)"),
    # LINE: LINE_ALBUM_..._20240315..., LINE_P20240315_..., LINE_MOVIE_... (first 8-digit date)
    ("line", MESSENGER, r"^LINE_.*?(?<!\d)" + _D8 + r"(?!\d)"),
)

# anywhere in the name: 2024-03-15, 20240315, 2024.03.15, 2024_03_15 (+ an optional time right after)
GENERAL = (r"(?<!\d)(?P<y>(?:19|20)\d{2})(?P<sep>[-._]?)(?P<m>0[1-9]|1[0-2])(?P=sep)(?P<d>0[1-9]|[12]\d|3[01])"
           r"(?:[ _T-](?P<H>[01]\d|2[0-3])[.:\-]?(?P<M>[0-5]\d)[.:\-]?(?P<S>[0-5]\d))?(?!\d)")


@dataclasses.dataclass
class NameDate:
    when: datetime.datetime
    has_time: bool
    source: str  # one of SOURCES ("" when the general pattern found it)
    multiple: bool = False  # the name holds more than one date: the first is used


@dataclasses.dataclass
class Compiled:
    builtin: list[tuple[str, str, re.Pattern]]
    user: list[re.Pattern]
    general: re.Pattern


_cache: dict[tuple, Compiled] = {}


def compile_user(patterns: list[str]) -> tuple[list[re.Pattern], list[str]]:
    """User patterns that compile and have y, m and d groups; the rest are returned as bad."""
    ok, bad = [], []
    for text in patterns:
        try:
            rx = re.compile(text, re.IGNORECASE)
        except re.error:
            bad.append(text)
            continue
        if not {"y", "m", "d"} <= set(rx.groupindex):
            bad.append(text)
            continue
        ok.append(rx)
    return ok, bad


def compiled(user_patterns: tuple[str, ...] = ()) -> Compiled:
    key = tuple(user_patterns)
    c = _cache.get(key)
    if c is None:
        c = Compiled([(n, s, re.compile(r, re.IGNORECASE)) for n, s, r in BUILTIN],
                     compile_user(list(user_patterns))[0], re.compile(GENERAL))
        _cache[key] = c
    return c


def _to_date(m: re.Match, years: tuple[int, int]) -> tuple[datetime.datetime, bool] | None:
    g = m.groupdict()
    try:
        y, mo, d = int(g["y"]), int(g["m"]), int(g["d"])
        has_time = g.get("H") is not None and g.get("M") is not None
        h = int(g["H"]) if has_time else 0
        mi = int(g["M"]) if has_time else 0
        s = int(g["S"]) if has_time and g.get("S") is not None else 0
        when = datetime.datetime(y, mo, d, h, mi, s)
    except (TypeError, ValueError, KeyError):
        return None  # 20241345, 20240231: not a date
    if not years[0] <= y <= years[1]:
        return None
    return when, has_time


def from_name(stem: str, years: tuple[int, int], user_patterns: tuple[str, ...] = ()) -> NameDate | None:
    """The date in a file name (without extension), or None."""
    c = compiled(user_patterns)
    general = [d for d in (_to_date(m, years) for m in c.general.finditer(stem)) if d]
    multiple = len({d[0].date() for d in general}) > 1
    for _name, source, rx in c.builtin:
        m = rx.search(stem)
        if m:
            got = _to_date(m, years)
            if got:
                return NameDate(got[0], got[1], source, multiple)
    for rx in c.user:
        m = rx.search(stem)
        if m:
            got = _to_date(m, years)
            if got:
                return NameDate(got[0], got[1], "", multiple)
    if general:
        return NameDate(general[0][0], general[0][1], "", multiple)
    return None


def source_of(stem: str) -> str:
    """Where a name says the file came from ("" when it does not say)."""
    for _name, source, rx in compiled().builtin:
        if rx.search(stem):
            return source
    return ""
