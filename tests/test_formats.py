"""PRD "파일 형식 관련" and the HEIC -> JPG conversion checks of the 완료 기준."""
from __future__ import annotations

import datetime
import errno
import os
import unicodedata

import piexif
import pytest
from conftest import build, by_name, files_under, ms, snapshot, without_log
from PIL import Image, ImageCms

import convert
import dates
import mover
import plan as plan_mod
import prefs as prefs_mod
import scan
import undo

D = datetime.datetime
CONVERT = prefs_mod.Prefs(convert=True)


def heic_with(path, orientation=6, size=(60, 40), icc=None, frames=1, taken="2024:03:15 12:34:56"):
    """A landscape picture whose left half is red and right half blue."""
    import pillow_heif

    pillow_heif.register_heif_opener()
    im = Image.new("RGB", size, (0, 0, 255))
    im.paste((255, 0, 0), (0, 0, size[0] // 2, size[1]))
    kw = {"exif": ms.exif_bytes(taken, orientation=orientation, make="Apple", model="iPhone 15", gps=True),
          "quality": 95}
    if icc:
        kw["icc_profile"] = icc
    if frames > 1:
        kw["save_all"] = True
        kw["append_images"] = [Image.new("RGB", size, (0, 255, 0)) for _ in range(frames - 1)]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path, format="HEIF", **kw)
    return path


# ------------------------------------------------------------------ conversion quality
def test_converted_jpg_is_upright_and_keeps_exif_and_gps(tmp_path):
    src = heic_with(str(tmp_path / "IMG_0001.HEIC"))
    out = convert.convert_file(src, str(tmp_path / "IMG_0001.jpg"))
    assert out.ok, out.error
    os.replace(out.dst + ".part", out.dst)
    with Image.open(out.dst) as im:
        assert im.format == "JPEG"
        assert im.size == (40, 60)  # turned upright: portrait
        r, g, b = im.convert("RGB").getpixel((20, 5))
        assert r > 200 and b < 60  # the red half is now on top (90 degrees clockwise)
        exif = piexif.load(im.info["exif"])
    assert exif["0th"][piexif.ImageIFD.Orientation] == 1
    assert exif["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"2024:03:15 12:34:56"
    assert exif["0th"][piexif.ImageIFD.Model] == b"iPhone 15"
    assert exif["GPS"][piexif.GPSIFD.GPSLatitude] == ((37, 1), (33, 1), (59, 1))
    assert dates.read_meta(out.dst, "JPEG", os.path.getsize(out.dst)).exif_original == "2024:03:15 12:34:56"


def test_exif_for_the_jpg_says_upright_and_has_no_old_thumbnail():
    """The decoder may already have reset Orientation; the JPG must say 1 whatever the source said."""
    raw = ms.exif_bytes("2024:03:15 12:34:56", orientation=6)
    out, rejected = convert._clean_exif(raw[6:] if raw.startswith(b"Exif") else raw, 40, 60)
    data = piexif.load(out)
    assert data["0th"][piexif.ImageIFD.Orientation] == 1 and not rejected
    assert (data["Exif"][piexif.ExifIFD.PixelXDimension], data["Exif"][piexif.ExifIFD.PixelYDimension]) == (40, 60)
    assert data["thumbnail"] is None


def test_icc_profile_kept_or_converted(tmp_path):
    p3 = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()  # any non-sRGB profile
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    src = heic_with(str(tmp_path / "a.HEIC"), icc=srgb)
    kept = convert.convert_file(src, str(tmp_path / "a.jpg"))
    with Image.open(kept.dst + ".part") as im:
        assert im.info.get("icc_profile") == srgb
    src2 = heic_with(str(tmp_path / "b.HEIC"), icc=srgb)
    conv = convert.convert_file(src2, str(tmp_path / "b.jpg"), icc_mode="srgb")
    assert conv.ok, conv.error
    with Image.open(conv.dst + ".part") as im:
        assert im.info.get("icc_profile")
    assert p3 != srgb


def test_quality_setting_is_used_and_clamped(tmp_path):
    src = heic_with(str(tmp_path / "a.HEIC"), size=(400, 300))
    sizes = {}
    for q in (80, 100, 5):
        out = convert.convert_file(src, str(tmp_path / f"q{q}.jpg"), quality=q)
        sizes[q] = out.out_size
    assert sizes[80] < sizes[100]
    assert sizes[5] == sizes[80]  # below the range: 80


def test_burst_heic_converts_the_first_image(tmp_path):
    src = heic_with(str(tmp_path / "burst.HEIC"), orientation=1, frames=3)
    out = convert.convert_file(src, str(tmp_path / "burst.jpg"))
    assert out.ok and out.burst
    with Image.open(out.dst + ".part") as im:
        assert im.convert("RGB").getpixel((5, 5))[0] > 200  # the red/blue first frame, not the green ones
    assert dates.read_meta(src, "HEIC", os.path.getsize(src)).frames == 3


def test_unreadable_heic_fails_conversion_but_still_moves(tmp_path):
    folder = tmp_path / "in1"
    bad = folder / "IMG_0009.HEIC"
    os.makedirs(folder)
    data = open(heic_with(str(tmp_path / "ok.HEIC")), "rb").read()
    bad.write_bytes(data[:200])  # cut short: the header says HEIC, the image is gone
    os.utime(bad, (D(2022, 2, 2).timestamp(),) * 2)
    _, p = build(folder, prefs=prefs_mod.Prefs(convert=True, use_mtime=True))
    item = by_name(p, "IMG_0009.HEIC")
    assert item.convert_dst
    res = mover.execute(p)
    assert os.path.exists(item.dst) and not os.path.exists(item.convert_dst)
    assert len(res.failed) == 1 and res.converted == 0 and res.done == 1
    assert files_under(folder) == ["2022/2022-02-02/IMG_0009.HEIC", "organize_log.json"]


def test_rejected_exif_tags_are_dropped_and_reported(tmp_path, monkeypatch):
    src = heic_with(str(tmp_path / "a.HEIC"))
    real_dump = piexif.dump

    def picky(data):
        if piexif.ImageIFD.Model in data.get("0th", {}):
            raise ValueError("refused")
        return real_dump(data)

    monkeypatch.setattr(piexif, "dump", picky)
    out = convert.convert_file(src, str(tmp_path / "a.jpg"))
    assert out.ok
    assert out.rejected_tags == ["0th:Model"]
    with Image.open(out.dst + ".part") as im:
        exif = piexif.load(im.info["exif"])
    assert piexif.ImageIFD.Model not in exif["0th"]
    assert exif["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"2024:03:15 12:34:56"


def test_bigger_jpg_is_kept_and_sizes_are_reported(sample_folder):
    _, p = build(sample_folder, prefs=CONVERT)
    res = mover.execute(p)
    assert res.converted == 1 and res.bytes_in > 0 and res.bytes_out > 0  # tiny HEIC: the JPG is larger, still kept
    assert os.path.exists(by_name(p, "IMG_0001.HEIC").convert_dst)


def test_converted_jpg_is_writable_even_from_a_read_only_heic(tmp_path):
    folder = tmp_path / "in1"
    src = heic_with(str(folder / "IMG_0003.HEIC"))
    os.chmod(src, 0o444)
    try:
        _, p = build(folder, prefs=CONVERT)
        res = mover.execute(p)
        assert not res.failed
        jpg = by_name(p, "IMG_0003.HEIC").convert_dst
        assert os.access(jpg, os.W_OK)
        assert not os.access(by_name(p, "IMG_0003.HEIC").dst, os.W_OK)  # the HEIC keeps its attribute
    finally:
        for here, _, fs in os.walk(folder):
            for f in fs:
                os.chmod(os.path.join(here, f), 0o666)


# ------------------------------------------------------------------ formats
def test_wrong_extension_is_read_by_its_bytes(tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "really_png.jpg"), "PNG")
    ms.photo(str(folder / "really_jpeg.heic"), exif=ms.exif_bytes("2023:04:04 04:04:04"))
    res = scan.scan(str(folder))
    fmts = {m.name: m.fmt for m in res.media}
    assert fmts == {"really_png.jpg": "PNG", "really_jpeg.heic": "JPEG"}
    _, p = build(folder, prefs=CONVERT)
    item = by_name(p, "really_jpeg.heic")
    assert item.when == D(2023, 4, 4, 4, 4, 4)
    assert item.dst.endswith("really_jpeg.heic")  # the extension is never changed
    assert item.convert_dst == ""  # it is not a HEIF: nothing to convert


def test_live_photo_pair_moves_together_and_only_the_heic_converts(sample_folder):
    _, p = build(sample_folder, prefs=CONVERT)
    item = by_name(p, "IMG_0001.HEIC")
    res = mover.execute(p)
    assert not res.failed
    day = os.path.join(str(sample_folder), "2024", "2024-03-15")
    assert sorted(os.listdir(day)) == ["IMG_0001.AAE", "IMG_0001.HEIC", "IMG_0001.MOV", "IMG_0001.jpg"]
    assert item.convert_dst == os.path.join(day, "IMG_0001.jpg")


def test_pair_in_different_folders_is_not_a_pair(tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "a" / "IMG_0004.HEIC"), "HEIF", ms.exif_bytes("2024:01:01 10:00:00"))
    ms.video(str(folder / "b" / "IMG_0004.MOV"), local="2024-05-05T10:00:00+0900")
    _, p = build(folder)
    heic, mov = by_name(p, "IMG_0004.HEIC"), by_name(p, "IMG_0004.MOV")
    assert heic is not mov
    assert "2024-05-05" in mov.dst and "2024-01-01" in heic.dst


def test_raw_and_jpg_pair_moves_together_and_is_not_a_duplicate(tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "DSC_0001.DNG"), "TIFF", ms.exif_bytes("2023:07:07 07:07:07", make="NIKON", model="Z 6"))
    ms.photo(str(folder / "DSC_0001.JPG"), exif=ms.exif_bytes("2023:07:07 07:07:07"))
    ms.photo(str(folder / "DSC_0001.xmp"), "PNG")  # any bytes
    _, p = build(folder, prefs=CONVERT)
    item = by_name(p, "DSC_0001.JPG")
    assert [m.name for m in item.members] == ["DSC_0001.DNG", "DSC_0001.JPG", "DSC_0001.xmp"]
    assert item.status == plan_mod.OK and item.convert_dst == ""
    assert item.basis == dates.B_EXIF and item.source == "camera"


def test_sidecar_without_its_photo_stays(tmp_path):
    folder = tmp_path / "in1"
    os.makedirs(folder)
    (folder / "IMG_0100.AAE").write_bytes(b"<plist/>")
    (folder / "IMG_0101.xmp").write_bytes(b"<x/>")
    _, p = build(folder)
    assert {(i.primary.name, i.status, i.acts) for i in p.items} == {("IMG_0100.AAE", plan_mod.SAME, False),
                                                                     ("IMG_0101.xmp", plan_mod.SAME, False)}


def test_animated_gif_and_webp_are_dated_not_converted(tmp_path):
    folder = tmp_path / "in1"
    os.makedirs(folder)
    frames = [ms.picture((40, 30), s) for s in (1, 2, 3)]
    frames[0].save(folder / "anim_20240101.gif", save_all=True, append_images=frames[1:])
    frames[0].save(folder / "anim_20240102.webp", save_all=True, append_images=frames[1:])
    _, p = build(folder, prefs=CONVERT)
    for name, day in (("anim_20240101.gif", "2024-01-01"), ("anim_20240102.webp", "2024-01-02")):
        item = by_name(p, name)
        assert day in item.dst and item.convert_dst == ""


def test_png_screenshot_by_name_or_no_date(tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "Screenshot_20240318_101112.png"), "PNG")
    ms.photo(str(folder / "capture.png"), "PNG")
    _, p = build(folder)
    assert by_name(p, "Screenshot_20240318_101112.png").when == D(2024, 3, 18, 10, 11, 12)
    assert by_name(p, "capture.png").status == plan_mod.NODATE


def test_zero_byte_file_is_no_date_with_a_note(sample_folder):
    _, p = build(sample_folder)
    item = by_name(p, "empty.jpg")
    assert item.status == plan_mod.NODATE and "empty" in item.notes and not item.acts


def test_long_target_name_is_shortened(tmp_path):
    folder = tmp_path / "in1"
    long_name = "20240315_101010_" + "x" * 200 + ".jpg"
    ms.photo(str(folder / long_name))
    _, p = build(folder)
    item = by_name(p, long_name)
    assert item.truncated and len(item.dst) <= plan_mod.MAX_PATH_LEN
    assert item.dst.endswith(".jpg")
    res = mover.execute(p)
    assert not res.failed and os.path.exists(item.dst)


LONG_PREFIX = "\\\\?\\"


@pytest.fixture
def no_long_paths(monkeypatch):
    """Act like a PC with Windows long paths off (the default on many; this one has them on, so
    without this a missing fs() would go unnoticed): a plain path of 260+ characters fails."""
    import builtins

    def too_long(p) -> bool:
        if not isinstance(p, (str, os.PathLike)):
            return False
        s = os.fspath(p)
        return isinstance(s, str) and not s.startswith(LONG_PREFIX) and len(os.path.abspath(s)) >= 260

    def guard(fn, both=False):
        def inner(*args, **kwargs):
            for p in args[: 2 if both else 1]:
                if too_long(p):
                    raise FileNotFoundError(errno.ENOENT, "long paths are off", os.fspath(p))
            return fn(*args, **kwargs)
        return inner

    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    for name in ("scandir", "stat", "lstat", "remove", "mkdir", "listdir", "chmod", "utime", "rmdir"):
        monkeypatch.setattr(os, name, guard(getattr(os, name)))
    for name in ("rename", "replace"):
        monkeypatch.setattr(os, name, guard(getattr(os, name), both=True))
    for name in ("isdir", "isfile", "exists", "lexists", "getsize"):
        monkeypatch.setattr(os.path, name, guard(getattr(os.path, name)))


def test_long_source_path_is_reached_with_the_long_form(tmp_path, no_long_paths):
    """A folder deeper than 260 characters: scanned, organized, converted and undone."""
    folder = tmp_path / "in1"
    deep = str(folder)
    while len(deep) < 280:
        deep = os.path.join(deep, "a_rather_long_folder_name_for_testing")
    os.makedirs(mover.fs(deep))
    data = open(heic_with(str(tmp_path / "x.HEIC")), "rb").read()
    with open(mover.fs(os.path.join(deep, "IMG_0001.HEIC")), "wb") as f:
        f.write(data)
    with open(mover.fs(os.path.join(deep, "IMG_0001.MOV")), "wb") as f:
        f.write(open(ms.video(str(tmp_path / "x.MOV"), local="2024-03-15T12:34:56+0900"), "rb").read())
    before = snapshot_long(folder)
    _, p = build(folder, prefs=CONVERT)
    item = by_name(p, "IMG_0001.HEIC")
    assert not p.denied and item.when == D(2024, 3, 15, 12, 34, 56)
    res = mover.execute(p)
    assert not res.failed, res.failed
    assert os.path.exists(item.convert_dst) and os.path.exists(item.dst)
    u = undo.undo(undo.find_log(str(folder)))
    assert not u.skipped
    assert without_log(snapshot_long(folder))["files"] == before["files"]


def snapshot_long(root) -> dict:
    import hashlib

    files = {}
    for here, ds, fs in os.walk(mover.fs(str(root))):
        for f in fs:
            p = os.path.join(here, f)
            files[os.path.relpath(mover.plain(p), str(root))] = hashlib.sha1(open(p, "rb").read()).hexdigest()
    return {"files": files, "dirs": []}


def test_nfd_names_pair_with_nfc_and_keep_their_spelling(tmp_path):
    folder = tmp_path / "in1"
    nfc = unicodedata.normalize("NFC", "여행_20240315")
    nfd = unicodedata.normalize("NFD", "여행_20240315")
    ms.photo(str(folder / (nfd + ".HEIC")), "HEIF")
    ms.video(str(folder / (nfc + ".MOV")), local="2024-03-15T10:00:00+0900")
    ms.photo(str(folder / ("emoji_" + chr(0x1F600) + "_20240316.jpg")))
    _, p = build(folder)
    item = by_name(p, nfd + ".HEIC")
    assert [m.name for m in item.members] == [nfd + ".HEIC", nfc + ".MOV"]
    assert os.path.basename(item.dsts[0]) == nfd + ".HEIC"  # the name itself is kept
    res = mover.execute(p)
    assert not res.failed
    assert "emoji_" + chr(0x1F600) + "_20240316.jpg" in os.listdir(os.path.join(str(folder), "2024", "2024-03-16"))


def test_nfc_and_nfd_twins_in_one_target_get_numbered(tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "a" / (unicodedata.normalize("NFC", "사진") + "_20240315.jpg")), seed=1)
    ms.photo(str(folder / "b" / (unicodedata.normalize("NFD", "사진") + "_20240315.jpg")), seed=2)
    _, p = build(folder)
    names = sorted(unicodedata.normalize("NFC", os.path.basename(i.dst)) for i in p.items)
    assert names == ["사진_20240315 (2).jpg", "사진_20240315.jpg"]


def test_system_clutter_is_ignored_and_does_not_keep_folders(tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "old" / "IMG_20240101_101010.jpg"))
    for junk in ("Thumbs.db", "desktop.ini", ".DS_Store", "._IMG_20240101_101010.jpg"):
        (folder / "old" / junk).write_bytes(b"x")
    res = scan.scan(str(folder))
    assert [m.name for m in res.media] == ["IMG_20240101_101010.jpg"]
    _, p = build(folder)
    assert p.empty_dirs == [str(folder / "old")]
    out = mover.execute(p)
    assert out.folders_removed == 1 and not os.path.exists(folder / "old")


def test_avif_option_follows_pillow_support():
    assert isinstance(convert.avif_supported(), bool)


@pytest.mark.skipif(not convert.avif_supported(), reason="this Pillow has no AVIF")
def test_avif_converts_upright_when_turned_on(tmp_path):
    """Pillow's AVIF reader leaves Orientation 6 for us to apply (pillow-heif's HEIC reader does it itself)."""
    folder = tmp_path / "in1"
    os.makedirs(folder)
    im = Image.new("RGB", (60, 40), (0, 0, 255))
    im.paste((255, 0, 0), (0, 0, 30, 40))
    im.save(folder / "IMG_0001.avif", exif=ms.exif_bytes("2024:03:15 12:34:56", orientation=6), quality=90)
    _, off = build(folder, prefs=CONVERT)
    assert by_name(off, "IMG_0001.avif").convert_dst == ""  # AVIF only when its own option is on
    _, p = build(folder, prefs=prefs_mod.Prefs(convert=True, convert_avif=True))
    item = by_name(p, "IMG_0001.avif")
    assert item.convert_dst and item.when == D(2024, 3, 15, 12, 34, 56)
    assert not mover.execute(p).failed
    with Image.open(item.convert_dst) as jpg:
        assert jpg.size == (40, 60) and jpg.convert("RGB").getpixel((20, 5))[0] > 200
        assert piexif.load(jpg.info["exif"])["0th"][piexif.ImageIFD.Orientation] == 1


@pytest.mark.parametrize("ext", [".heic", ".heif", ".hif"])
def test_every_heif_extension_converts(tmp_path, ext):
    folder = tmp_path / "in1"
    heic_with(str(folder / ("IMG_0001" + ext)))
    _, p = build(folder, prefs=CONVERT)
    assert by_name(p, "IMG_0001" + ext).convert_dst.endswith("IMG_0001.jpg")
