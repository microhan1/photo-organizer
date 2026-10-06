"""PRD "날짜 관련": one test per row, plus the filename pattern table."""
from __future__ import annotations

import datetime
import json
import os

import pytest
from conftest import build, by_name, ms

import dates
import mover
import patterns
import plan as plan_mod
import scan

D = datetime.datetime
OPTS = dates.DateOptions()


def resolve(meta=None, stem="IMG_0001", mtime=0.0, opts=OPTS):
    return dates.resolve(meta or dates.Meta(), stem, mtime, opts)


# ------------------------------------------------------------------ filename patterns (3 samples each)
@pytest.mark.parametrize("stem, when, source", [
    ("KakaoTalk_20240315_123456789", D(2024, 3, 15, 12, 34, 56), patterns.KAKAO),
    ("KakaoTalk_Photo_2024-03-15-12-34-56", D(2024, 3, 15, 12, 34, 56), patterns.KAKAO),
    ("KakaoTalk_Video_2023-12-31-23-59-58", D(2023, 12, 31, 23, 59, 58), patterns.KAKAO),
    ("20240315_123456", D(2024, 3, 15, 12, 34, 56), patterns.CAMERA),
    ("20240315_123456(1)", D(2024, 3, 15, 12, 34, 56), patterns.CAMERA),
    ("20231001_080910", D(2023, 10, 1, 8, 9, 10), patterns.CAMERA),
    ("PXL_20240315_123456789", D(2024, 3, 15, 12, 34, 56), patterns.CAMERA),
    ("IMG_20240315_123456", D(2024, 3, 15, 12, 34, 56), patterns.CAMERA),
    ("VID_20220707_070707", D(2022, 7, 7, 7, 7, 7), patterns.CAMERA),
    ("IMG-20240315-WA0001", D(2024, 3, 15), patterns.MESSENGER),
    ("band_20240315", D(2024, 3, 15), patterns.MESSENGER),
    ("LINE_ALBUM_Trip_20240315_1", D(2024, 3, 15), patterns.MESSENGER),
    ("Screenshot_20240315_123456", D(2024, 3, 15, 12, 34, 56), patterns.SCREENSHOT),
    ("스크린샷 2024-03-15 123456", D(2024, 3, 15, 12, 34, 56), patterns.SCREENSHOT),
    ("Screen Shot 2024-03-15 at 12.34.56", D(2024, 3, 15, 12, 34, 56), patterns.SCREENSHOT),
    ("Screenshot_2024-03-15-12-34-56", D(2024, 3, 15, 12, 34, 56), patterns.SCREENSHOT),
    ("trip 2024-03-15", D(2024, 3, 15), ""),
    ("scan20240315", D(2024, 3, 15), ""),
    ("2024.03.15 family", D(2024, 3, 15), ""),
])
def test_filename_patterns(stem, when, source):
    got = patterns.from_name(stem, (1990, 2100))
    assert got is not None, stem
    assert (got.when, got.source) == (when, source)


@pytest.mark.parametrize("stem", ["IMG_1234", "DSC_0001", "photo", "20241345", "20240231_0000", "img_19890101"])
def test_names_without_a_usable_date(stem):
    assert patterns.from_name(stem, (1990, 2026)) is None


def test_iphone_names_are_dated_by_exif_and_live_photo_video(sample_folder):
    _, p = build(sample_folder)
    item = by_name(p, "IMG_0001.HEIC")
    assert (item.when, item.basis) == (D(2024, 3, 15, 12, 34, 56), dates.B_EXIF)
    assert [m.name for m in item.members] == ["IMG_0001.HEIC", "IMG_0001.MOV", "IMG_0001.AAE"]


def test_user_pattern_from_settings():
    opts = dates.DateOptions(user_patterns=(r"^MyCam_(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})",))
    assert resolve(stem="MyCam_20240102_x", opts=opts).when == D(2024, 1, 2)
    bad = patterns.compile_user(["(unclosed", r"^no_groups_\d+"])[1]
    assert bad == ["(unclosed", r"^no_groups_\d+"]


# ------------------------------------------------------------------ PRD rows
@pytest.mark.parametrize("text", ["0000:00:00 00:00:00", "", "    ", "\x00\x00"])
def test_zero_or_blank_exif_date_falls_through(text):
    r = resolve(dates.Meta(exif_original=text), stem="KakaoTalk_20240315_123456789")
    assert (r.when, r.basis) == (D(2024, 3, 15, 12, 34, 56), dates.B_NAME)


def test_camera_clock_reset_prefers_the_name():
    r = resolve(dates.Meta(exif_original="2000:01:01 00:00:00"), stem="20240315_101010")
    assert (r.when, r.basis) == (D(2024, 3, 15, 10, 10, 10), dates.B_NAME)


def test_camera_clock_reset_without_name_date_is_marked_suspect():
    r = resolve(dates.Meta(exif_original="2000:01:01 00:00:00"))
    assert (r.when, r.basis) == (D(2000, 1, 1), dates.B_EXIF_SUSPECT)


def test_1970_reset_outside_the_year_range_is_not_used():
    r = resolve(dates.Meta(exif_original="1970:01:01 00:00:00"))
    assert r.when is None


@pytest.mark.parametrize("text", ["2099:01:01 10:00:00", "1985:05:05 10:00:00"])
def test_future_or_too_old_exif_is_ignored(text):
    r = resolve(dates.Meta(exif_original=text, exif_datetime="2021:06:01 09:00:00"))
    assert (r.when, r.basis) == (D(2021, 6, 1, 9), dates.B_EXIF)


def test_year_range_comes_from_settings():
    opts = dates.DateOptions(year_min=1980)
    assert resolve(dates.Meta(exif_original="1985:05:05 10:00:00"), opts=opts).when == D(1985, 5, 5, 10)


@pytest.mark.parametrize("text, when", [
    ("2024-03-15T12:34:56", D(2024, 3, 15, 12, 34, 56)),
    ("2024-03-15 12:34:56", D(2024, 3, 15, 12, 34, 56)),
    ("2024:03:15 12:34", D(2024, 3, 15, 12, 34)),
    ("2024/03/15 12:34:56", D(2024, 3, 15, 12, 34, 56)),
    ("2024:03:15 12:34:56+09:00", D(2024, 3, 15, 12, 34, 56)),
])
def test_nonstandard_exif_formats(text, when):
    assert dates.parse_exif_datetime(text) == when


def test_unparsable_exif_falls_through():
    r = resolve(dates.Meta(exif_original="yesterday"), stem="IMG_20240315_101010")
    assert r.basis == dates.B_NAME


def test_digitized_and_datetime_are_second_choice():
    assert resolve(dates.Meta(exif_digitized="2023:02:02 02:02:02")).when == D(2023, 2, 2, 2, 2, 2)
    assert resolve(dates.Meta(exif_datetime="2023:03:03 03:03:03")).when == D(2023, 3, 3, 3, 3, 3)


def test_broken_exif_does_not_stop_the_file(sample_folder):
    _, p = build(sample_folder)
    item = by_name(p, "broken_exif.jpg")
    assert item.status == plan_mod.NODATE  # read, not crashed; it just has no date


def test_pillow_exception_counts_as_no_exif(tmp_path, monkeypatch):
    path = ms.photo(str(tmp_path / "a.png"), "PNG")

    def boom(_p):
        raise SyntaxError("broken")

    monkeypatch.setattr(dates, "_pillow_exif", boom)
    meta = dates.read_meta(path, "PNG", os.path.getsize(path))
    assert meta.error and meta.exif_original == ""


def test_two_dates_in_the_name_use_the_first():
    r = resolve(stem="20240315_copy_20240320")
    assert (r.when, r.basis) == (D(2024, 3, 15), dates.B_NAME_MULTI)


def test_invalid_name_date_is_no_match():
    assert resolve(stem="IMG_20241345").when is None


def test_exif_beats_kakaotalk_name():
    r = resolve(dates.Meta(exif_original="2022:02:22 22:22:22"), stem="KakaoTalk_20240315_123456789")
    assert (r.when, r.basis, r.source) == (D(2022, 2, 22, 22, 22, 22), dates.B_EXIF, patterns.KAKAO)


@pytest.mark.parametrize("utc", [None, 0])
def test_video_creation_time_zero_is_no_date(tmp_path, utc):
    path = ms.video(str(tmp_path / "clip.mp4"), utc=utc, brand=b"isom")
    meta = dates.read_meta(path, "MP4", os.path.getsize(path))
    assert meta.video_utc is None and meta.video_local is None


def test_video_utc_is_shown_in_local_time(tmp_path):
    stamp = int(D(2024, 3, 15, 3, 34, 56).replace(tzinfo=datetime.timezone.utc).timestamp())
    path = ms.video(str(tmp_path / "clip.mp4"), utc=stamp, brand=b"isom")
    meta = dates.read_meta(path, "MP4", os.path.getsize(path))
    assert meta.video_utc == D(2024, 3, 15, 3, 34, 56)
    r = resolve(meta, stem="clip")
    assert r.when == datetime.datetime.fromtimestamp(stamp) and r.basis == dates.B_VIDEO


def test_iphone_mov_local_creationdate_wins_over_utc(tmp_path):
    utc = int(D(2024, 3, 14, 15, 30).replace(tzinfo=datetime.timezone.utc).timestamp())
    path = ms.video(str(tmp_path / "IMG_0002.MOV"), utc=utc, local="2024-03-15T00:30:00+0900")
    meta = dates.read_meta(path, "MOV", os.path.getsize(path))
    assert resolve(meta, stem="IMG_0002").when == D(2024, 3, 15, 0, 30)


def test_video_day_atom_and_64bit_mvhd(tmp_path):
    path = ms.video(str(tmp_path / "a.mov"), utc=1710473696, day="2024-03-15T12:00:00+0900", mvhd_version=1)
    meta = dates.read_meta(path, "MOV", os.path.getsize(path))
    assert meta.video_local == D(2024, 3, 15, 12) and meta.video_utc is not None


def test_exif_just_after_midnight_is_not_shifted(tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "night.jpg"), exif=ms.exif_bytes("2024:03:16 00:10:00"))
    _, p = build(folder)
    assert os.path.basename(os.path.dirname(by_name(p, "night.jpg").dst)) == "2024-03-16"


def test_manual_date_moves_only_that_file_and_is_logged(sample_folder):
    _, first = build(sample_folder)
    item = by_name(first, "IMG_1234.jpg")
    ov = plan_mod.Overrides(dates={item.key: D(2021, 5, 5, 5, 5)})
    _, p = build(sample_folder, overrides=ov)
    fixed = by_name(p, "IMG_1234.jpg")
    assert fixed.basis == dates.B_MANUAL
    assert fixed.dst.replace(os.sep, "/").endswith("2021/2021-05-05/IMG_1234.jpg")
    assert by_name(p, "KakaoTalk_20240316_083000123.jpg").dst == by_name(first, "KakaoTalk_20240316_083000123.jpg").dst
    before = open(fixed.primary.path, "rb").read()
    res = mover.execute(p)
    assert not res.failed
    assert open(fixed.dst, "rb").read() == before  # EXIF (and every byte) untouched
    log = json.load(open(res.log_path, encoding="utf-8"))
    op = next(o for o in log["runs"][-1]["ops"] if o.get("dst") == fixed.dst)
    assert op["manual_date"] == "2021-05-05T05:05:00"


def test_pair_with_different_exif_dates_uses_the_heic(tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "IMG_0005.HEIC"), "HEIF", ms.exif_bytes("2024:01:01 10:00:00"))
    ms.photo(str(folder / "IMG_0005.JPG"), exif=ms.exif_bytes("2024:06:06 10:00:00"))
    _, p = build(folder)
    item = by_name(p, "IMG_0005.JPG")
    assert item.primary.name == "IMG_0005.HEIC" and item.when == D(2024, 1, 1, 10)
    assert {os.path.dirname(d) for d in item.dsts} == {os.path.join(str(folder), "2024", "2024-01-01")}


def test_pair_borrows_the_date_when_the_photo_has_none(tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "IMG_0007.HEIC"), "HEIF")
    ms.video(str(folder / "IMG_0007.MOV"), local="2023-08-08T08:08:08+0900")
    _, p = build(folder)
    item = by_name(p, "IMG_0007.HEIC")
    assert (item.when, item.basis) == (D(2023, 8, 8, 8, 8, 8), dates.B_PAIR)


def test_mtime_only_when_turned_on(tmp_path):
    folder = tmp_path / "in1"
    path = ms.photo(str(folder / "IMG_1234.jpg"))
    stamp = D(2020, 2, 2, 2, 2, 2).timestamp()
    os.utime(path, (stamp, stamp))
    import prefs as prefs_mod

    _, off = build(folder)
    assert by_name(off, "IMG_1234.jpg").status == plan_mod.NODATE
    _, on = build(folder, prefs=prefs_mod.Prefs(use_mtime=True))
    item = by_name(on, "IMG_1234.jpg")
    assert (item.when, item.basis) == (D(2020, 2, 2, 2, 2, 2), dates.B_MTIME)


def test_exif_read_from_only_the_first_segments(tmp_path):
    """A JPEG's EXIF is found by walking segment headers, not by reading the image."""
    path = ms.photo(str(tmp_path / "big.jpg"), exif=ms.exif_bytes("2024:03:15 12:34:56"), size=(3000, 2000))
    calls = []
    real_open = open

    class Spy:
        def __init__(self, f):
            self.f = f

        def read(self, n=-1):
            calls.append(n)
            return self.f.read(n)

        def __getattr__(self, name):
            return getattr(self.f, name)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.f.close()

    import builtins

    builtins_open = builtins.open
    try:
        builtins.open = lambda p, mode="r", *a, **k: Spy(real_open(p, mode, *a, **k)) if p == path else real_open(p, mode, *a, **k)
        meta = dates.read_meta(path, "JPEG", os.path.getsize(path))
    finally:
        builtins.open = builtins_open
    assert meta.exif_original == "2024:03:15 12:34:56"
    assert all(0 <= n <= 64 * 1024 for n in calls)


def test_scan_reads_every_sample_without_error(sample_folder):
    res = scan.scan(str(sample_folder))
    assert len(res.media) == 14
    assert [m.name for m in res.media if m.meta.error] == []
