"""Long file names that collide: the music tool's planning once never ended because the " (2)" that tells
two files apart was cut off by the path-length limit (its LESSONS A27). Here the number is added after the
name is shortened; these tests keep it that way, with names and folders right at the limit."""
from __future__ import annotations

import os
import re
import threading

import pytest
from conftest import build, files_under, ms, snapshot, without_log

import mover
import plan as plan_mod
import scan
import prefs as prefs_mod
import undo


def photos(folder, count: int, stem_len: int, subfolders: bool = True, ext: str = ".jpg") -> list[str]:
    """``count`` different pictures taken the same minute, all with one very long name (in different folders)."""
    paths = []
    for n in range(count):
        sub = folder / f"d{n}" if subfolders else folder
        name = ("N" * stem_len) + ext
        if not subfolders:
            name = ("N" * (stem_len - 4)) + f"{n:04d}" + ext  # one folder: the names must differ
        paths.append(ms.photo(str(sub / name), exif=ms.exif_bytes("2024:03:15 12:00:00"), size=(24, 18), seed=n + 1))
    return paths


def plan_with_timeout(folder, seconds: int = 30, **kw):
    box = {}
    t = threading.Thread(target=lambda: box.update(res=build(folder, **kw)), daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "planning never ended"
    return box["res"]


@pytest.mark.parametrize("stem_len", [60, 150, 200, 240])
def test_colliding_long_names_get_distinct_numbers_and_stay_quiet(tmp_path, stem_len):
    folder = tmp_path / "in1"
    photos(folder, 5, stem_len)
    before = snapshot(folder)
    _, p = plan_with_timeout(folder)
    dsts = [i.dst for i in p.items]
    assert len({scan.target_key(d) for d in dsts}) == 5, [os.path.basename(d) for d in dsts]
    assert all(len(d) <= 259 for d in dsts), max(map(len, dsts))
    numbers = sorted(m.group(1) if (m := re.search(r" \((\d+)\)$", os.path.splitext(os.path.basename(d))[0])) else "" for d in dsts)
    assert numbers == ["", "2", "3", "4", "5"], numbers  # the number is never the part that gets cut
    res = mover.execute(p)
    assert not res.failed, res.failed
    _, again = build(folder)
    assert again.actionable() == 0, [(i.primary.name, i.status) for i in again.items if i.acts]
    assert not undo.undo(undo.find_log(str(folder))).skipped
    assert without_log(snapshot(folder)) == without_log(before)


def test_destination_so_deep_that_names_cannot_shrink_further_still_numbers(tmp_path):
    """The folder path alone nearly fills the limit: the name is already at its minimum, only the number can
    tell the files apart."""
    folder = tmp_path / "in1"
    deep = tmp_path / "out" / ("x" * 60) / ("y" * 60) / ("z" * 60)
    photos(folder, 4, 120)
    _, p = plan_with_timeout(folder, dest=deep)
    dsts = [i.dst for i in p.items]
    assert len({scan.target_key(d) for d in dsts}) == 4
    res = mover.execute(p)
    assert not res.failed, res.failed  # the long-path form (fs) carries what the plain form could not
    assert len([f for f in files_under(deep) if f.endswith(".jpg")]) == 4


def test_long_names_with_the_rename_option_and_a_converted_pair(tmp_path):
    """The rename prefix (2024-03-15_123456_) and a HEIC + its converted JPG both lengthen the name."""
    folder = tmp_path / "in1"
    for n in range(3):
        ms.photo(str(folder / f"d{n}" / (("H" * 180) + ".HEIC")), "HEIF", ms.exif_bytes("2024:03:15 12:00:00"),
                 size=(24, 18), seed=n + 1)
    p0 = prefs_mod.Prefs(rename=True, convert=True)
    _, p = plan_with_timeout(folder, prefs=p0)
    keys = {scan.target_key(x) for i in p.items for x in (*i.dsts, i.convert_dst) if x}
    assert len(keys) == 6  # three HEICs and three JPGs, all different
    assert all(len(x) <= 259 for i in p.items for x in (*i.dsts, i.convert_dst) if x)
    res = mover.execute(p)
    assert not res.failed, res.failed
    _, again = build(folder, prefs=p0)
    assert again.actionable() == 0, [(i.primary.name, i.status) for i in again.items if i.acts]


def test_numbering_resumes_fast_for_thousands_of_long_equal_names(tmp_path):
    """5,000 files with one long name in one date folder: numbering must not restart from (2) for each."""
    import time

    folder = tmp_path / "in1"
    data = open(ms.photo(str(tmp_path / "t.jpg"), exif=ms.exif_bytes("2024:03:15 12:00:00"), size=(16, 12)), "rb").read()
    for n in range(5000):
        d = folder / f"d{n:04d}"
        os.makedirs(d)
        (d / (("L" * 150) + ".jpg")).write_bytes(data + n.to_bytes(4, "big"))  # same name, different bytes
    start = time.perf_counter()
    _, p = plan_with_timeout(folder, seconds=120, prefs=prefs_mod.Prefs(dedupe=False), dedupe=False)
    assert time.perf_counter() - start < 60
    assert len({scan.target_key(i.dst) for i in p.items}) == 5000
