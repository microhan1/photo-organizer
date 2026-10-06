"""PRD "중복 관련" and the pHash itself."""
from __future__ import annotations

import os
import shutil

from conftest import build, by_name, ms
from PIL import Image, ImageDraw

import dedupe
import mover
import plan as plan_mod
import prefs as prefs_mod
import scan

SIMILAR = prefs_mod.Prefs(similar=True)


def burst_frame(n: int, size=(320, 240)) -> Image.Image:
    """One scene; a small ball moves a little in every frame."""
    im = ms.picture(size, seed=42)
    d = ImageDraw.Draw(im)
    d.ellipse((100 + 4 * n, 60, 120 + 4 * n, 80), fill=(250, 220, 0))
    return im


# ------------------------------------------------------------------ pHash
def test_phash_survives_rotation_and_recompression():
    base = ms.picture((320, 240), seed=7)
    h = dedupe.phash_image(base)
    for angle in (90, 180, 270):
        turned = dedupe.phash_image(base.rotate(angle, expand=True))
        assert min(dedupe.distance(turned[0], x) for x in h) <= 2, angle
    small = base.resize((160, 120))
    assert dedupe.distance(dedupe.phash_image(small)[0], h[0]) <= 4
    other = dedupe.phash_image(ms.picture((320, 240), seed=8))
    assert min(dedupe.distance(other[0], x) for x in h) > dedupe.DEFAULT_THRESHOLD


def test_burst_fixture_is_really_close():
    """Check the test data first (sibling repos lost hours to wrong fixtures)."""
    hashes = [dedupe.phash_image(burst_frame(n))[0] for n in range(10)]
    assert max(dedupe.distance(hashes[0], h) for h in hashes) <= dedupe.DEFAULT_THRESHOLD


# ------------------------------------------------------------------ exact
def test_identical_files_one_stays_kakao_copy_leaves(tmp_path):
    folder = tmp_path / "in1"
    original = ms.photo(str(folder / "camera" / "IMG_0001.jpg"), exif=ms.exif_bytes("2024:03:15 12:00:00"))
    os.makedirs(folder / "chat")
    shutil.copy2(original, folder / "chat" / "KakaoTalk_20240315_120000123.jpg")
    shutil.copy2(original, folder / "chat" / "copy.jpg")
    _, p = build(folder)
    keep = by_name(p, "IMG_0001.jpg")
    assert keep.status == plan_mod.OK
    for name in ("KakaoTalk_20240315_120000123.jpg", "copy.jpg"):
        item = by_name(p, name)
        assert item.status == plan_mod.DUP and item.dup_of == original
        assert os.path.dirname(item.dst) == os.path.join(str(folder), prefs_mod.i18n.t("folder_dupes"))
    assert not mover.execute(p).failed
    _, again = build(folder)
    assert again.actionable() == 0  # the duplicates folder is not scanned again


def test_duplicates_with_the_same_name_are_numbered_in_the_dupes_folder(tmp_path):
    folder = tmp_path / "in1"
    original = ms.photo(str(folder / "a" / "x.jpg"), exif=ms.exif_bytes("2024:03:15 12:00:00"))
    for sub in ("b", "c"):
        os.makedirs(folder / sub)
        shutil.copy2(original, folder / sub / "x.jpg")
    _, p = build(folder)
    dupes = sorted(os.path.basename(i.dst) for i in p.items if i.status == plan_mod.DUP)
    assert dupes == ["x (2).jpg", "x.jpg"]


def test_duplicates_to_the_recycle_bin_when_chosen(tmp_path, monkeypatch):
    folder = tmp_path / "in1"
    original = ms.photo(str(folder / "a" / "x.jpg"), exif=ms.exif_bytes("2024:03:15 12:00:00"))
    os.makedirs(folder / "b")
    shutil.copy2(original, folder / "b" / "x.jpg")
    trashed = []
    monkeypatch.setattr(mover, "trash", lambda p: trashed.append(p) or os.remove(p))
    _, p = build(folder, prefs=prefs_mod.Prefs(dupes_action="trash"))
    dup = next(i for i in p.items if i.status == plan_mod.DUP)
    assert dup.trashes and dup.dst == ""
    res = mover.execute(p)
    assert res.trashed == 1 and trashed == [dup.primary.path]


def test_dedupe_off_finds_nothing(tmp_path):
    folder = tmp_path / "in1"
    original = ms.photo(str(folder / "a" / "x.jpg"))
    os.makedirs(folder / "b")
    shutil.copy2(original, folder / "b" / "y.jpg")
    _, p = build(folder, dedupe=False)
    assert all(i.status != plan_mod.DUP for i in p.items)


# ------------------------------------------------------------------ pairs
def test_heic_and_its_jpg_are_a_pair_not_duplicates(tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "IMG_0001.HEIC"), "HEIF", ms.exif_bytes("2024:03:15 12:00:00"))
    ms.photo(str(folder / "IMG_0001.JPG"), exif=ms.exif_bytes("2024:03:15 12:00:00"))
    _, p = build(folder, similar=True, prefs=SIMILAR)
    assert len(p.items) == 1 and p.items[0].status == plan_mod.OK and p.items[0].similar == 0


def test_jpg_of_a_pair_as_duplicate_when_set(tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "IMG_0001.HEIC"), "HEIF", ms.exif_bytes("2024:03:15 12:00:00"))
    ms.photo(str(folder / "IMG_0001.JPG"), exif=ms.exif_bytes("2024:03:15 12:00:00"))
    _, p = build(folder, prefs=prefs_mod.Prefs(jpg_pair_as_dupe=True))
    heic, jpg = by_name(p, "IMG_0001.HEIC"), by_name(p, "IMG_0001.JPG")
    assert heic is not jpg and jpg.status == plan_mod.DUP and jpg.dup_reason == "pair"
    assert heic.status == plan_mod.OK


# ------------------------------------------------------------------ similar
def test_burst_is_grouped_without_a_suggestion(tmp_path):
    folder = tmp_path / "in1"
    for n in range(10):
        ms.photo(str(folder / f"IMG_{n:04d}.jpg"), exif=ms.exif_bytes(f"2024:03:15 12:00:{n:02d}"),
                 image=burst_frame(n))
    _, p = build(folder, similar=True, prefs=SIMILAR)
    groups = {i.similar for i in p.items}
    assert len(groups) == 1 and 0 not in groups
    assert not any(i.suggest_dup for i in p.items)
    assert all(i.status == plan_mod.OK for i in p.items)  # shown only: nothing leaves on its own


def test_kakaotalk_recompression_is_the_suggested_duplicate(tmp_path):
    folder = tmp_path / "in1"
    base = ms.picture((640, 480), seed=11)
    ms.photo(str(folder / "IMG_0001.jpg"), exif=ms.exif_bytes("2024:03:15 12:00:00"), image=base, quality=95)
    ms.photo(str(folder / "KakaoTalk_20240316_080000123.jpg"), image=base.resize((320, 240)), quality=70)
    _, p = build(folder, similar=True, prefs=SIMILAR)
    kakao, orig = by_name(p, "KakaoTalk_20240316_080000123.jpg"), by_name(p, "IMG_0001.jpg")
    assert kakao.similar == orig.similar != 0
    assert kakao.suggest_dup and not orig.suggest_dup


def test_kakaotalk_copy_at_the_same_size_is_still_the_suggested_duplicate(tmp_path):
    """KakaoTalk keeps the size of small photos but recompresses and strips EXIF."""
    folder = tmp_path / "in1"
    base = ms.picture((640, 480), seed=15)
    ms.photo(str(folder / "IMG_0002.jpg"), exif=ms.exif_bytes("2024:03:15 12:00:00"), image=base, quality=95)
    ms.photo(str(folder / "KakaoTalk_20240320_090000123.jpg"), image=base, quality=70)
    _, p = build(folder, similar=True, prefs=SIMILAR)
    kakao, orig = by_name(p, "KakaoTalk_20240320_090000123.jpg"), by_name(p, "IMG_0002.jpg")
    assert kakao.similar == orig.similar != 0
    assert kakao.suggest_dup and not orig.suggest_dup


def test_rotated_copy_is_similar_and_the_bigger_one_is_kept(tmp_path):
    folder = tmp_path / "in1"
    base = ms.picture((640, 480), seed=12)
    ms.photo(str(folder / "big.jpg"), image=base)
    ms.photo(str(folder / "turned_small.jpg"), image=base.rotate(90, expand=True).resize((240, 320)))
    _, p = build(folder, similar=True, prefs=SIMILAR)
    big, small = by_name(p, "big.jpg"), by_name(p, "turned_small.jpg")
    assert big.similar == small.similar != 0
    assert small.suggest_dup and not big.suggest_dup


def test_resave_with_other_exif_suggests_the_smaller_file(tmp_path):
    folder = tmp_path / "in1"
    base = ms.picture((400, 300), seed=13)
    ms.photo(str(folder / "a.jpg"), exif=ms.exif_bytes("2024:03:15 12:00:00", model="X"), image=base, quality=95)
    ms.photo(str(folder / "b.jpg"), exif=ms.exif_bytes("2024:03:15 12:00:00", model="Editor"), image=base, quality=80)
    _, p = build(folder, similar=True, prefs=SIMILAR)
    a, b = by_name(p, "a.jpg"), by_name(p, "b.jpg")
    assert a.status != plan_mod.DUP and b.status != plan_mod.DUP  # not byte-identical
    assert a.similar == b.similar != 0
    assert b.suggest_dup and not a.suggest_dup


def test_marking_and_keeping_by_hand(tmp_path):
    folder = tmp_path / "in1"
    base = ms.picture((400, 300), seed=14)
    ms.photo(str(folder / "a.jpg"), image=base)
    ms.photo(str(folder / "b.jpg"), image=base.resize((200, 150)))
    result, first = build(folder, similar=True, prefs=SIMILAR)
    a, b = by_name(first, "a.jpg"), by_name(first, "b.jpg")
    ov = plan_mod.Overrides(dup={b.key: True})
    _, p = build(folder, similar=True, prefs=SIMILAR, overrides=ov)
    assert by_name(p, "b.jpg").status == plan_mod.DUP and not p.blocked_groups()
    ov.dup[a.key] = True  # every member leaving: not allowed
    _, p2 = build(folder, similar=True, prefs=SIMILAR, overrides=ov)
    assert p2.blocked_groups()


def test_exact_duplicate_kept_by_hand_is_not_moved(tmp_path):
    folder = tmp_path / "in1"
    original = ms.photo(str(folder / "a" / "x.jpg"), exif=ms.exif_bytes("2024:03:15 12:00:00"))
    os.makedirs(folder / "b")
    shutil.copy2(original, folder / "b" / "x.jpg")
    _, first = build(folder)
    dup = next(i for i in first.items if i.status == plan_mod.DUP)
    _, p = build(folder, overrides=plan_mod.Overrides(dup={dup.key: False}))
    names = sorted(os.path.relpath(i.dst, folder).replace(os.sep, "/") for i in p.items)
    assert names == ["2024/2024-03-15/x (2).jpg", "2024/2024-03-15/x.jpg"]  # both kept, the second numbered


def test_estimate_scales_with_count(sample_folder):
    _, p = build(sample_folder)
    est = dedupe.estimate_seconds(p.items)
    assert 0 < est < 5


def test_cancel_similar_returns_nothing(sample_folder):
    import threading

    _, p = build(sample_folder)
    ev = threading.Event()
    ev.set()
    assert dedupe.find_similar(p.items, cancel=ev) == {}


def test_hashes_are_cached_on_the_media(sample_folder):
    res = scan.scan(str(sample_folder))
    opts = prefs_mod.options(prefs_mod.Prefs(), str(sample_folder))
    p = plan_mod.build(res, opts)
    dedupe.find_similar(p.items)
    assert all(i.primary.phash for i in p.items if i.primary.kind == "jpeg" and i.primary.size)
