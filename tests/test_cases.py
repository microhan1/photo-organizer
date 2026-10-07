"""Hand-designed scenarios the random tester cannot reach: things users do between and during runs."""
from __future__ import annotations

import datetime
import json
import os
import shutil
import stat
import subprocess
import sys
import threading

import pytest
from conftest import ROOT, build, by_name, files_under, ms, snapshot, without_log

import dates
import i18n
import main
import mover
import patterns
import plan as plan_mod
import prefs as prefs_mod
import scan
import undo

D = datetime.datetime


def jpg(path, date="2024:03:15 12:00:00", seed=1, **kw):
    return ms.photo(str(path), exif=ms.exif_bytes(date), size=(24, 18), seed=seed, **kw)


# ------------------------------------------------------------------ copy mode
def test_copy_then_copy_again_does_not_pile_up_duplicates(tmp_path):
    """Running the same copy twice must not fill the duplicates folder with the copies already made."""
    src = tmp_path / "in1"
    for n in range(3):
        jpg(src / f"IMG_{n}.jpg", f"2024:03:1{n} 10:00:00", seed=n)
    out = tmp_path / "out"
    _, p1 = build(src, dest=out, mode="copy")
    assert not mover.execute(p1).failed
    _, p2 = build(src, dest=out, mode="copy")
    assert p2.actionable() == 0, [(i.primary.name, i.status, i.dst) for i in p2.items if i.acts]


# ------------------------------------------------------------------ language
@pytest.mark.parametrize("first, second", [("ko", "en"), ("en", "ja"), ("zh-CN", "ko")])
def test_switching_language_does_not_reshuffle_special_folders(tmp_path, first, second):
    """The No Date and Duplicates folders are named by language: a run in one language must still be
    'nothing to do' after the language is switched."""
    folder = tmp_path / "in1"
    base = jpg(folder / "a" / "x.jpg")
    os.makedirs(folder / "b")
    shutil.copy2(base, folder / "b" / "x.jpg")  # an exact duplicate
    ms.photo(str(folder / "c" / "nodate.jpg"), size=(24, 18), seed=9)
    p = prefs_mod.Prefs(include_nodate=True)
    i18n.set_lang(first, persist=False)
    _, p1 = build(folder, prefs=p)
    assert not mover.execute(p1).failed
    i18n.set_lang(second, persist=False)
    _, p2 = build(folder, prefs=p)
    moved = [(i.primary.name, os.path.relpath(i.dst, folder)) for i in p2.items if i.acts]
    assert moved == [], f"{first}->{second}: {moved}"


# ------------------------------------------------------------------ undo, as users use it
def organized(tmp_path, n=4, convert=False):
    folder = tmp_path / "in1"
    for k in range(n):
        jpg(folder / f"IMG_{k}.jpg", f"2024:03:1{k} 10:00:00", seed=k)
    if convert:
        ms.photo(str(folder / "IMG_H.HEIC"), "HEIF", ms.exif_bytes("2024:05:05 10:00:00"), size=(24, 18), seed=50)
    before = snapshot(folder)
    _, p = build(folder, prefs=prefs_mod.Prefs(convert=convert))
    res = mover.execute(p)
    assert not res.failed
    return folder, p, before


def test_undo_twice_is_harmless(tmp_path):
    folder, _, before = organized(tmp_path)
    assert not undo.undo(undo.find_log(str(folder))).skipped
    again = undo.undo(undo.find_log(str(folder)))
    assert again.nothing and again.restored == 0
    assert without_log(snapshot(folder)) == without_log(before)


def test_user_deleted_a_moved_file_before_undo(tmp_path):
    folder, p, before = organized(tmp_path)
    gone = by_name(p, "IMG_1.jpg").dst
    os.remove(gone)
    u = undo.undo(undo.find_log(str(folder)))
    assert [os.path.basename(s) for s, _ in u.skipped] == ["IMG_1.jpg"] and u.restored == 3
    assert "IMG_1.jpg" not in files_under(folder)


def test_original_place_taken_by_a_new_file_keeps_both(tmp_path):
    folder, p, before = organized(tmp_path)
    (folder / "IMG_2.jpg").write_bytes(b"the user saved something new here")
    u = undo.undo(undo.find_log(str(folder)))
    assert len(u.skipped) == 1 and u.restored == 3
    assert (folder / "IMG_2.jpg").read_bytes() == b"the user saved something new here"
    assert os.path.exists(by_name(p, "IMG_2.jpg").dst)  # the moved file is where it was, not lost


def test_user_edited_a_converted_jpg_before_undo_keeps_the_edit(tmp_path):
    folder, p, _ = organized(tmp_path, convert=True)
    out = by_name(p, "IMG_H.HEIC").convert_dst
    with open(out, "ab") as f:
        f.write(b"edited")
    u = undo.undo(undo.find_log(str(folder)))
    assert any(os.path.basename(s) == "IMG_H.jpg" for s, _ in u.skipped)
    assert os.path.exists(out)  # the user's edited JPG is not deleted


def test_two_runs_undone_newest_first_and_oldest_is_blocked(tmp_path):
    folder = tmp_path / "in1"
    jpg(folder / "IMG_0.jpg", "2024:03:10 10:00:00")
    _, p1 = build(folder)
    mover.execute(p1)
    first_dst = by_name(p1, "IMG_0.jpg").dst
    _, p2 = build(folder, prefs=prefs_mod.Prefs(pattern="{yyyy-mm-dd}", rename=True))  # reorganise: a second run
    r2 = mover.execute(p2)
    assert r2.done == 1
    runs = undo.runs_in([undo.find_log(str(folder))])
    assert len(runs) == 2
    oldest = runs[-1]
    blocked = undo.undo(oldest.log, run_id=oldest.id)
    assert blocked.blocked_by and blocked.restored == 0  # the newer run moved that file again
    assert undo.undo(undo.find_log(str(folder))).restored == 1  # newest first
    assert os.path.exists(first_dst)
    assert undo.undo(undo.find_log(str(folder))).restored == 1
    assert (folder / "IMG_0.jpg").exists()


# ------------------------------------------------------------------ stopping and crashing
def test_cancel_mid_run_then_run_again_finishes_and_undo_restores_all(tmp_path):
    folder = tmp_path / "in1"
    for k in range(30):
        jpg(folder / f"IMG_{k:02d}.jpg", f"2024:03:{1 + k % 28:02d} 10:00:00", seed=k)
    before = snapshot(folder)
    _, p = build(folder)
    cancel = threading.Event()
    res = mover.execute(p, progress=lambda d, t: cancel.set() if d == 10 else None, cancel=cancel)
    assert res.cancelled and 0 < res.done < 30
    _, rest = build(folder)
    assert 0 < rest.summary()["move"] == 30 - res.done  # exactly the rest
    assert not mover.execute(rest).failed
    _, none = build(folder)
    assert none.actionable() == 0
    for _ in range(2):  # two runs recorded: newest first
        assert not undo.undo(undo.find_log(str(folder))).skipped
    assert without_log(snapshot(folder)) == without_log(before)


def test_crash_in_the_middle_leaves_a_log_that_undoes_what_moved(tmp_path, monkeypatch):
    folder = tmp_path / "in1"
    for k in range(8):
        jpg(folder / f"IMG_{k}.jpg", f"2024:03:1{k} 10:00:00", seed=k)
    before = snapshot(folder)
    _, p = build(folder)
    real, calls = mover.move_file, {"n": 0}

    def move(a, b):
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("the program crashed")  # not an OSError: nothing catches it per file
        return real(a, b)

    monkeypatch.setattr(mover, "move_file", move)
    with pytest.raises(RuntimeError):
        mover.execute(p)
    monkeypatch.setattr(mover, "move_file", real)
    u = undo.undo(undo.find_log(str(folder)))
    assert u.restored == 3 and not u.skipped
    assert without_log(snapshot(folder)) == without_log(before)


def test_killed_program_leaves_a_journal_that_undo_merges(tmp_path):
    """A hard kill never reaches flush(): only the journal has the steps."""
    folder = tmp_path / "in1"
    for k in range(5):
        jpg(folder / f"IMG_{k}.jpg", f"2024:03:1{k} 10:00:00", seed=k)
    before = snapshot(folder)
    _, p = build(folder)
    log = mover.RunLog(str(folder), str(folder), "move")
    for item in p.items[:3]:
        mover.make_dirs(os.path.dirname(item.dst), log)
        mover.move_file(item.primary.path, item.dst)
        log.add(op="move", src=item.primary.path, dst=item.dst)
    log._journal.close()  # killed: no flush(), no final save
    assert os.path.exists(mover.journal_path(log.path))
    u = undo.undo(undo.find_log(str(folder)))
    assert u.restored == 3 and not u.skipped
    assert without_log(snapshot(folder)) == without_log(before)


# ------------------------------------------------------------------ the disk changes under us
def test_file_deleted_between_preview_and_run_fails_alone(tmp_path):
    folder = tmp_path / "in1"
    for k in range(3):
        jpg(folder / f"IMG_{k}.jpg", f"2024:03:1{k} 10:00:00", seed=k)
    _, p = build(folder)
    os.remove(folder / "IMG_1.jpg")
    res = mover.execute(p)
    assert res.done == 2 and len(res.failed) == 1
    assert not os.path.exists(folder / "2024" / "2024-03-11")  # no folder was made for the vanished file


def test_a_file_named_like_a_target_folder_blocks_only_that_item(tmp_path):
    folder = tmp_path / "in1"
    jpg(folder / "IMG_A.jpg", "2024:03:10 10:00:00", seed=1)
    jpg(folder / "IMG_B.jpg", "2023:03:10 10:00:00", seed=2)
    (folder / "2024").write_bytes(b"I am a file, not a folder")
    _, p = build(folder)
    res = mover.execute(p)
    assert res.done == 1 and len(res.failed) == 1
    assert os.path.exists(folder / "2023" / "2023-03-10" / "IMG_B.jpg") and os.path.exists(folder / "IMG_A.jpg")
    assert (folder / "2024").read_bytes() == b"I am a file, not a folder"
    assert not undo.undo(undo.find_log(str(folder))).skipped


def test_target_appears_after_the_preview_is_not_overwritten(tmp_path):
    folder = tmp_path / "in1"
    jpg(folder / "IMG_A.jpg", "2024:03:10 10:00:00", seed=1)
    _, p = build(folder)
    target = by_name(p, "IMG_A.jpg").dst
    os.makedirs(os.path.dirname(target))
    open(target, "wb").write(b"someone else's file")
    res = mover.execute(p)
    assert len(res.failed) == 1 and open(target, "rb").read() == b"someone else's file"
    assert os.path.exists(folder / "IMG_A.jpg")


def test_file_vanishing_while_scanning_does_not_break_the_scan(tmp_path):
    folder = tmp_path / "in1"
    path = jpg(folder / "IMG_A.jpg")
    st = os.stat(path)
    os.remove(path)
    m = scan.read_media(path, st)  # listed, then gone before it was opened
    assert m.meta.error and m.name == "IMG_A.jpg"


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
def test_folder_junction_loop_does_not_hang_the_scan(tmp_path):
    folder = tmp_path / "in1"
    jpg(folder / "a" / "IMG_A.jpg")
    out = subprocess.run(["cmd", "/c", "mklink", "/J", str(folder / "a" / "loop"), str(folder)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        pytest.skip("cannot create a junction here")
    box = {}
    t = threading.Thread(target=lambda: box.update(res=scan.scan(str(folder))), daemon=True)
    t.start()
    t.join(20)
    assert not t.is_alive(), "the scan followed the junction forever"
    assert [m.name for m in box["res"].media] == ["IMG_A.jpg"]  # found once, not once per lap


def test_symlink_to_a_folder_is_not_followed(tmp_path):
    folder = tmp_path / "in1"
    jpg(folder / "real" / "IMG_A.jpg")
    try:
        os.symlink(str(folder / "real"), str(folder / "link"), target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need privileges here")
    assert [m.name for m in scan.scan(str(folder)).media] == ["IMG_A.jpg"]


# ------------------------------------------------------------------ names and dates people really have
def test_case_only_twins_in_different_folders_both_survive(tmp_path):
    folder = tmp_path / "in1"
    jpg(folder / "a" / "IMG_1.JPG", "2024:03:10 10:00:00", seed=1)
    jpg(folder / "b" / "img_1.jpg", "2024:03:10 11:00:00", seed=2)
    _, p = build(folder)
    names = sorted(os.path.basename(i.dst).lower() for i in p.items)
    assert names == ["img_1 (2).jpg", "img_1.jpg"]
    assert not mover.execute(p).failed
    assert build(folder)[1].actionable() == 0


def test_rename_option_survives_a_second_run_and_a_new_date(tmp_path):
    folder = tmp_path / "in1"
    jpg(folder / "KakaoTalk_20240315_123456789.jpg", "2024:03:15 12:34:56")
    p = prefs_mod.Prefs(rename=True, pattern="{yyyy}/{source}/{yyyy-mm-dd}")
    _, p1 = build(folder, prefs=p)
    mover.execute(p1)
    first = files_under(folder)
    assert first[0] == "2024/KakaoTalk/2024-03-15/2024-03-15_123456_KakaoTalk_20240315_123456789.jpg" or \
        any(f.endswith("2024-03-15_123456_KakaoTalk_20240315_123456789.jpg") for f in first)
    assert build(folder, prefs=p)[1].actionable() == 0  # nothing moves: "source" is still KakaoTalk
    # the user fixes the date by hand: the prefix is replaced, not stacked
    item = build(folder, prefs=p)[1].items[0]
    ov = plan_mod.Overrides(dates={item.key: D(2020, 1, 2, 3, 4, 5)})
    _, p2 = build(folder, prefs=p, overrides=ov)
    mover.execute(p2)
    final = [f for f in files_under(folder) if f.endswith(".jpg")]
    assert final == ["2020/KakaoTalk/2020-01-02/2020-01-02_030405_KakaoTalk_20240315_123456789.jpg"], final


def test_prefix_of_the_rename_option_is_understood_by_the_name_reader():
    got = patterns.from_name("2019-07-01_080000_band_20240121_19", (1990, 2030))
    assert got and got.when == D(2019, 7, 1, 8, 0, 0) and got.source == patterns.MESSENGER
    assert patterns.source_of("2023-09-06_200145_Screenshot_20230906_200145") == patterns.SCREENSHOT
    assert patterns.strip_renamed("2024-13-45_999999_x") == "2024-13-45_999999_x"  # not a real date: left alone


@pytest.mark.parametrize("text", ["2024:02:30 10:00:00", "2024:13:01 10:00:00", "2024:03:15 24:00:00",
                                  "2024:03:15 10:61:00", "2023:02:29 10:00:00", "2024:00:10 10:00:00"])
def test_impossible_exif_dates_are_not_dates(text):
    assert dates.parse_exif_datetime(text) is None


@pytest.mark.parametrize("text", ["2024:03:15 10:00:00 AM", "2024:03:15 10:00:00+09:00", " 2024:03:15 10:00:00\x00"])
def test_exif_dates_with_trailing_noise_still_read(text):
    assert dates.parse_exif_datetime(text) == D(2024, 3, 15, 10, 0, 0)


def test_leap_day_and_midnight_edges_land_in_the_right_folder(tmp_path):
    folder = tmp_path / "in1"
    jpg(folder / "a.jpg", "2024:02:29 23:59:59", seed=1)
    jpg(folder / "b.jpg", "2024:03:01 00:00:00", seed=2)
    _, p = build(folder)
    assert {os.path.basename(os.path.dirname(i.dst)) for i in p.items} == {"2024-02-29", "2024-03-01"}


def test_future_mtime_is_not_used_and_exif_wins_over_everything(tmp_path):
    folder = tmp_path / "in1"
    path = ms.photo(str(folder / "nodate.jpg"), size=(24, 18))
    future = (D.now() + datetime.timedelta(days=400)).timestamp()
    os.utime(path, (future, future))
    _, p = build(folder, prefs=prefs_mod.Prefs(use_mtime=True))
    assert by_name(p, "nodate.jpg").status == plan_mod.NODATE


def test_names_with_odd_characters_move_and_come_back(tmp_path):
    folder = tmp_path / "in1"
    names = ["a #1 [x] (y) %20 & z.jpg", "ünï cødé ñ 사진 写真.jpg", "emoji " + chr(0x1F600) + ".jpg", "  spaced  .jpg",
             "trailing.dot..jpg", "UPPER.JPG", "noext_but_date_20240315"]
    for n, name in enumerate(names):
        if name.startswith("noext"):
            continue
        jpg(folder / name, f"2024:03:{10 + n} 10:00:00", seed=n)
    before = snapshot(folder)
    _, p = build(folder, prefs=prefs_mod.Prefs(rename=True))
    assert not mover.execute(p).failed
    assert not undo.undo(undo.find_log(str(folder))).skipped
    assert without_log(snapshot(folder)) == without_log(before)


# ------------------------------------------------------------------ empty and odd folders (CLI)
def run_cli(tmp_path, *args):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PHOTO_ORGANIZER_SETTINGS=str(tmp_path / "s.json"))
    return subprocess.run([sys.executable, os.path.join(ROOT, "main.py"), *args, "--lang", "en"], capture_output=True,
                          text=True, encoding="utf-8", env=env, stdin=subprocess.DEVNULL, timeout=120,
                          cwd=str(tmp_path))


@pytest.mark.parametrize("make", ["empty", "only_dirs", "only_junk", "only_text"])
def test_folders_without_photos_are_reported_not_crashed(tmp_path, make):
    folder = tmp_path / "in1"
    os.makedirs(folder / "sub")
    if make == "only_junk":
        (folder / "sub" / "Thumbs.db").write_bytes(b"x")
    if make == "only_text":
        (folder / "notes.txt").write_text("hi")
    if make == "empty":
        shutil.rmtree(folder / "sub")
    out = run_cli(tmp_path, str(folder))
    assert out.returncode == 1 and "No photos" in out.stdout and "Traceback" not in out.stderr
    assert not (folder / "organize_log.json").exists()


@pytest.mark.parametrize("args, code", [(["{nofolder}"], 2), (["{file}"], 2), (["{folder}", "--copy"], 2),
                                        (["{folder}", "--pattern", "/abs/{yyyy}"], 2),
                                        (["{folder}", "--pattern", "../{yyyy}"], 2),
                                        (["{folder}", "--pattern", "{yyyy"], 2)])
def test_cli_rejects_bad_input_without_touching_files(tmp_path, args, code):
    folder = tmp_path / "in1"
    jpg(folder / "IMG_A.jpg")
    (tmp_path / "afile.txt").write_text("x")
    before = snapshot(tmp_path)
    names = {"{nofolder}": str(tmp_path / "missing"), "{file}": str(tmp_path / "afile.txt"), "{folder}": str(folder)}
    argv = [names.get(a, a) for a in args]  # (str.format would try to fill {yyyy} too)
    out = run_cli(tmp_path, *argv)
    assert out.returncode == code and "Traceback" not in out.stderr, out.stderr
    assert without_log(snapshot(tmp_path)) == without_log(before)


def test_cli_with_unicode_trailing_slash_and_relative_paths(tmp_path):
    folder = tmp_path / "사진 정리 폴더"
    jpg(folder / "IMG_A.jpg", "2024:03:10 10:00:00")
    for spelling in (str(folder) + os.sep, os.path.relpath(folder, tmp_path), str(folder)):
        out = run_cli(tmp_path, spelling, "--dry-run")
        assert out.returncode == 0 and "2024" in out.stdout, (spelling, out.stderr)


def test_cli_dest_that_does_not_exist_yet_is_created_and_undone(tmp_path):
    folder = tmp_path / "in1"
    jpg(folder / "IMG_A.jpg", "2024:03:10 10:00:00")
    dest = tmp_path / "new" / "deeper" / "out"
    out = run_cli(tmp_path, str(folder), "--dest", str(dest))
    assert out.returncode == 0, out.stderr
    assert os.path.exists(dest / "2024" / "2024-03-10" / "IMG_A.jpg")
    back = run_cli(tmp_path, str(folder), "--undo")
    assert back.returncode == 0, back.stderr
    assert (folder / "IMG_A.jpg").exists() and not (dest / "2024").exists()


# ------------------------------------------------------------------ settings people edit by hand
@pytest.mark.parametrize("content", ["not json", "[]", "null", '{"quality": "high"}', '{"pattern": 5, "convert": "yes"}',
                                     '{"quality": 5000, "year_min": -3, "theme": "neon", "name_patterns": [1, "(", "^ok_(?P<y>\\\\d{4})(?P<m>\\\\d{2})(?P<d>\\\\d{2})"]}',
                                     '{"pattern": "{nope}/x"}', ""])
def test_hand_edited_settings_fall_back_to_defaults(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="utf-8")
    i18n._settings_path = str(path)
    p = prefs_mod.load()
    default = prefs_mod.Prefs()
    assert p.quality == default.quality and p.theme == default.theme and p.pattern == default.pattern
    assert all(isinstance(s, str) for s in p.name_patterns)
    assert plan_mod.validate_pattern(p.pattern) == ""


# ------------------------------------------------------------------ option combinations that must stay quiet on a second run
def quiet_second_run(folder, prefs, dest=None):
    _, p1 = build(folder, dest=dest, prefs=prefs)
    res = mover.execute(p1)
    assert not [f for f in res.failed], res.failed
    _, p2 = build(folder, dest=dest, prefs=prefs)
    acts = [(i.primary.name, i.status, [os.path.relpath(d, dest or folder) for d in i.dsts if d]) for i in p2.items if i.acts]
    assert not acts, acts
    return p1


def test_ext_pattern_with_convert_and_moved_originals_is_stable(tmp_path):
    """{ext} named the folder after the HEIC; once the HEIC moved to the originals folder the lone JPG
    was filed under "jpg" on the next run."""
    folder = tmp_path / "in1"
    ms.photo(str(folder / "IMG_1.HEIC"), "HEIF", ms.exif_bytes("2024:03:15 12:00:00"), size=(24, 18), seed=1)
    ms.video(str(folder / "IMG_1.MOV"), local="2024-03-15T12:00:00+0900")
    p = prefs_mod.Prefs(pattern="by-ext/{ext}/{yyyy}", convert=True, heic_original="move")
    quiet_second_run(folder, p)
    assert (folder / "by-ext" / "jpg" / "2024" / "IMG_1.jpg").exists()
    assert (folder / "by-ext" / "jpg" / "2024" / "IMG_1.MOV").exists()


def test_convert_with_jpg_pair_as_duplicate_is_stable(tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "IMG_1.HEIC"), "HEIF", ms.exif_bytes("2024:03:15 12:00:00"), size=(24, 18), seed=1)
    jpg(folder / "IMG_1.JPG", "2024:03:15 12:00:00", seed=2)
    p1 = quiet_second_run(folder, prefs_mod.Prefs(convert=True, jpg_pair_as_dupe=True))
    assert p1.summary()["convert"] == 0  # the options contradict: converting is off


def test_the_date_of_a_pair_does_not_come_from_the_jpg_that_leaves_as_duplicate(tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "IMG_1.HEIC"), "HEIF", size=(24, 18), seed=1)  # no EXIF
    jpg(folder / "IMG_1.JPG", "2024:03:15 12:00:00", seed=2)
    ms.video(str(folder / "IMG_1.MOV"), utc=1600000000)  # 2020-09-13 (UTC)
    quiet_second_run(folder, prefs_mod.Prefs(jpg_pair_as_dupe=True))
    assert any("2020" in f for f in files_under(folder) if f.endswith("IMG_1.HEIC"))


def test_rename_with_source_folders_is_stable_for_every_known_source(tmp_path):
    folder = tmp_path / "in1"
    for name in ["KakaoTalk_20240315_123456789", "Screenshot_20240316_101010", "IMG-20240317-WA0001",
                 "band_20240318_5", "PXL_20240319_101010123", "LINE_ALBUM_x_20240320_1"]:
        ms.photo(str(folder / (name + ".jpg")), size=(24, 18), seed=len(name))
    quiet_second_run(folder, prefs_mod.Prefs(rename=True, pattern="{yyyy}/{source}/{yyyy-mm-dd}"))
    folders = sorted({os.path.dirname(f).split("/")[1] for f in files_under(folder) if f.endswith(".jpg")})
    assert folders == ["Camera", "KakaoTalk", "Messengers", "Screenshots"], folders


def test_copy_again_after_new_photos_copies_only_the_new_ones(tmp_path):
    src = tmp_path / "in1"
    out = tmp_path / "out"
    for n in range(3):
        jpg(src / f"IMG_{n}.jpg", f"2024:03:1{n} 10:00:00", seed=n)
    _, p1 = build(src, dest=out, mode="copy")
    mover.execute(p1)
    jpg(src / "IMG_new.jpg", "2024:04:01 10:00:00", seed=9)
    _, p2 = build(src, dest=out, mode="copy")
    assert [i.primary.name for i in p2.items if i.acts] == ["IMG_new.jpg"]
    mover.execute(p2)
    assert len([f for f in files_under(out) if f.endswith(".jpg")]) == 4
    assert not any("_Duplicates" in f for f in files_under(out))


def test_copy_again_keeps_numbered_twins_and_real_duplicates_quiet(tmp_path):
    src = tmp_path / "in1"
    out = tmp_path / "out"
    jpg(src / "a" / "IMG_1.jpg", "2024:03:10 10:00:00", seed=1)
    jpg(src / "b" / "IMG_1.jpg", "2024:03:10 11:00:00", seed=2)  # same name, other picture: becomes "IMG_1 (2)"
    shutil.copy2(src / "a" / "IMG_1.jpg", src / "c.jpg")  # an exact duplicate: goes to the duplicates folder
    _, p1 = build(src, dest=out, mode="copy")
    mover.execute(p1)
    first = files_under(out)
    _, p2 = build(src, dest=out, mode="copy")
    assert p2.actionable() == 0, [(i.primary.name, i.status) for i in p2.items if i.acts]
    mover.execute(p2) if p2.actionable() else None
    assert files_under(out) == first


def test_copy_when_the_target_has_different_bytes_under_the_same_name_still_numbers(tmp_path):
    src = tmp_path / "in1"
    out = tmp_path / "out"
    jpg(src / "IMG_1.jpg", "2024:03:10 10:00:00", seed=1)
    os.makedirs(out / "2024" / "2024-03-10")
    (out / "2024" / "2024-03-10" / "IMG_1.jpg").write_bytes(b"an unrelated file of the same name")
    _, p = build(src, dest=out, mode="copy")
    assert [os.path.basename(i.dst) for i in p.items] == ["IMG_1 (2).jpg"]
    assert not mover.execute(p).failed
    _, again = build(src, dest=out, mode="copy")
    assert again.actionable() == 0
