"""PRD "저장소·경로 관련", the undo round trip of the 완료 기준, and the CLI."""
from __future__ import annotations

import ctypes
import errno
import json
import os
import shutil
import subprocess
import sys

import pytest
from conftest import ROOT, build, by_name, files_under, ms, snapshot, without_log

import dates
import main
import mover
import plan as plan_mod
import prefs as prefs_mod
import scan
import undo

CONVERT = prefs_mod.Prefs(convert=True)
FILE_ATTRIBUTE_OFFLINE = 0x1000


def set_attr(path: str, attr: int) -> None:
    current = ctypes.windll.kernel32.GetFileAttributesW(path)
    assert ctypes.windll.kernel32.SetFileAttributesW(path, current | attr)


# ------------------------------------------------------------------ cloud, network, permissions
@pytest.mark.skipif(sys.platform != "win32", reason="Windows file attributes")
def test_cloud_only_files_are_skipped_and_never_opened(tmp_path, monkeypatch):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "IMG_20240101_101010.jpg"))
    cloud = ms.photo(str(folder / "IMG_20240102_101010.jpg"), seed=2)
    set_attr(cloud, FILE_ATTRIBUTE_OFFLINE)
    opened = []
    real_read_media = scan.read_media
    monkeypatch.setattr(scan, "read_media", lambda p, st: opened.append(p) or real_read_media(p, st))
    res = scan.scan(str(folder))
    assert [m.name for m in res.media] == ["IMG_20240101_101010.jpg"]
    assert res.cloud == [cloud]
    assert cloud not in opened
    _, p = build(folder)
    assert p.cloud == [cloud] and all(cloud != m.path for i in p.items for m in i.members)


def test_network_paths_are_recognised():
    assert scan.network_path(r"\\nas\photos")
    assert not scan.network_path(ROOT)


def test_unreadable_folder_is_skipped_with_a_warning(tmp_path, monkeypatch):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "ok" / "IMG_20240101_101010.jpg"))
    ms.photo(str(folder / "locked" / "IMG_20240102_101010.jpg"))
    real = os.scandir

    def scandir(path):
        if mover.plain(str(path)).endswith("locked"):
            raise PermissionError(13, "denied", path)
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    res = scan.scan(str(folder))
    assert [m.name for m in res.media] == ["IMG_20240101_101010.jpg"]
    assert res.denied == [str(folder / "locked")]


# ------------------------------------------------------------------ drives
def test_drive_pulled_mid_run_stops_and_can_be_undone(tmp_path, monkeypatch):
    src_root = tmp_path / "in1"
    for n in range(5):
        ms.photo(str(src_root / f"IMG_2024010{n + 1}_101010.jpg"), seed=n)
    dest = tmp_path / "out"
    _, p = build(src_root, dest=dest)
    real_move = mover.move_file
    calls = {"n": 0}

    def move(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:  # the source drive disappears before the third file
            os.rename(str(src_root), str(tmp_path / "unplugged"))
        return real_move(src, dst)

    monkeypatch.setattr(mover, "move_file", move)
    res = mover.execute(p)
    assert res.cancelled and res.done == 2
    assert any("unplugged" in r or "no longer" in r for _, r in res.failed)
    monkeypatch.setattr(mover, "move_file", real_move)
    os.rename(str(tmp_path / "unplugged"), str(src_root))  # plugged in again
    u = undo.undo(undo.find_log(str(dest)))
    assert u.restored == 2 and not u.skipped
    assert len(files_under(src_root)) == 5


def test_move_to_another_drive_copies_verifies_and_deletes(tmp_path, monkeypatch):
    folder = tmp_path / "in1"
    src = ms.photo(str(folder / "IMG_20240101_101010.jpg"))
    os.chmod(src, 0o444)  # read-only originals still leave
    data = open(src, "rb").read()
    real_rename = os.rename

    def rename(a, b):
        if os.path.normcase(mover.plain(a)) == os.path.normcase(src):
            raise OSError(errno.EXDEV, "cross-device")
        return real_rename(a, b)

    monkeypatch.setattr(os, "rename", rename)
    _, p = build(folder, dest=tmp_path / "out")
    res = mover.execute(p)
    assert not res.failed and not os.path.exists(src)
    dst = by_name(p, "IMG_20240101_101010.jpg").dst
    assert open(dst, "rb").read() == data
    os.chmod(dst, 0o666)


def test_copy_mismatch_keeps_the_original(tmp_path, monkeypatch):
    folder = tmp_path / "in1"
    src = ms.photo(str(folder / "IMG_20240101_101010.jpg"))
    _, p = build(folder, dest=tmp_path / "out")  # the scan hashes too: plan first, then break the copy check
    monkeypatch.setattr(os, "rename", lambda a, b: (_ for _ in ()).throw(OSError(errno.EXDEV, "x")))
    real_sha1 = mover.hashlib.sha1
    count = {"n": 0}

    class Liar:
        """The second hash of every pair (the re-read copy) comes out different."""

        def __init__(self, data=b""):
            count["n"] += 1
            self.h = real_sha1(data)
            self.lie = count["n"] % 2 == 0

        def update(self, b):
            self.h.update(b + (b"!" if self.lie else b""))

        def digest(self):
            return self.h.digest()

    monkeypatch.setattr(mover.hashlib, "sha1", Liar)
    res = mover.execute(p)
    assert len(res.failed) == 1 and os.path.exists(src)
    assert not os.path.exists(by_name(p, "IMG_20240101_101010.jpg").dst)


# ------------------------------------------------------------------ destination placement
def test_destination_inside_the_source_is_not_scanned_again(tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "IMG_20240101_101010.jpg"))
    dest = folder / "sorted"
    _, p = build(folder, dest=dest)
    mover.execute(p)
    res = scan.scan(str(folder), prefs_mod.special_folders(str(folder), str(dest)))
    assert res.media == []
    _, again = build(folder, dest=dest)
    assert again.actionable() == 0


def test_destination_above_the_source_is_allowed(tmp_path):
    folder = tmp_path / "top" / "in1"
    ms.photo(str(folder / "IMG_20240101_101010.jpg"))
    _, p = build(folder, dest=tmp_path / "top")
    res = mover.execute(p)
    assert not res.failed
    assert os.path.exists(tmp_path / "top" / "2024" / "2024-01-01" / "IMG_20240101_101010.jpg")
    assert os.path.isdir(folder)  # the source folder itself is never removed


def test_not_enough_space_blocks_the_run(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "IMG_20240101_101010.jpg"))
    before = snapshot(folder)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(10, 10, 1))
    _, p = build(folder, dest=tmp_path / "out", mode="copy")
    need, free = mover.space_check(p)
    assert need > free
    code = main.main([str(folder), "--dest", str(tmp_path / "out"), "--copy", "--lang", "en"])
    assert code == 1 and snapshot(folder) == before and not os.path.exists(tmp_path / "out")
    assert "space" in capsys.readouterr().err


def test_same_drive_move_needs_no_space(tmp_path, monkeypatch):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "IMG_20240101_101010.jpg"))
    _, p = build(folder)
    assert mover.space_check(p)[0] == 0


def test_read_only_file_moves_and_keeps_its_attribute(tmp_path):
    folder = tmp_path / "in1"
    src = ms.photo(str(folder / "IMG_20240101_101010.jpg"))
    os.chmod(src, 0o444)
    _, p = build(folder)
    res = mover.execute(p)
    dst = by_name(p, "IMG_20240101_101010.jpg").dst
    assert not res.failed and not os.access(dst, os.W_OK)
    os.chmod(dst, 0o666)


def test_file_open_elsewhere_fails_alone(tmp_path):
    folder = tmp_path / "in1"
    busy = ms.photo(str(folder / "IMG_20240101_101010.jpg"))
    ms.photo(str(folder / "IMG_20240102_101010.jpg"), seed=2)
    _, p = build(folder)
    with open(busy, "rb"):  # Windows: an open file cannot be renamed
        res = mover.execute(p)
    assert res.done == 1 and [os.path.basename(f) for f, _ in res.failed] == ["IMG_20240101_101010.jpg"]


def test_pair_member_failing_puts_the_other_back(tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "IMG_0001.HEIC"), "HEIF", ms.exif_bytes("2024:03:15 12:34:56"))
    mov = ms.video(str(folder / "IMG_0001.MOV"), local="2024-03-15T12:34:56+0900")
    _, p = build(folder)
    with open(mov, "rb"):
        res = mover.execute(p)
    assert res.done == 0 and len(res.failed) == 1
    assert files_under(folder) == ["IMG_0001.HEIC", "IMG_0001.MOV", "organize_log.json"]
    u = undo.undo(undo.find_log(str(folder)))  # the logged move and its move back cancel out
    assert not u.skipped
    assert files_under(folder) == ["IMG_0001.HEIC", "IMG_0001.MOV", "organize_log.json"]


# ------------------------------------------------------------------ names
def test_same_name_same_date_numbered_or_duplicate_and_stable(tmp_path):
    folder = tmp_path / "in1"
    a = ms.photo(str(folder / "a" / "IMG_0001.jpg"), exif=ms.exif_bytes("2024:05:05 10:00:00"), seed=1)
    ms.photo(str(folder / "b" / "IMG_0001.jpg"), exif=ms.exif_bytes("2024:05:05 11:00:00"), seed=2)
    os.makedirs(folder / "c")
    shutil.copy2(a, folder / "c" / "IMG_0001.jpg")  # same bytes as a
    _, p = build(folder, prefs=prefs_mod.Prefs(dedupe=False))
    result = sorted((os.path.relpath(i.dst, folder).replace(os.sep, "/"), i.status) for i in p.items)
    assert result == [("2024/2024-05-05/IMG_0001 (2).jpg", plan_mod.CONFLICT),
                      ("2024/2024-05-05/IMG_0001.jpg", plan_mod.OK),
                      (os.path.join(prefs_mod.i18n.t("folder_dupes"), "IMG_0001.jpg").replace(os.sep, "/"), plan_mod.DUP)]
    assert not mover.execute(p).failed
    _, again = build(folder, prefs=prefs_mod.Prefs(dedupe=False))
    assert again.actionable() == 0 and {i.status for i in again.items} == {plan_mod.SAME}


def test_five_thousand_same_names_plan_fast(tmp_path):
    """Worst case for numbering (organizer A3): one name, one date, thousands of files."""
    import time

    folder = tmp_path / "in1"
    template = ms.photo(str(tmp_path / "t.jpg"), size=(16, 12))
    data = open(template, "rb").read()
    for n in range(5000):
        d = folder / f"d{n:04d}"
        os.makedirs(d)
        (d / "IMG_20240101_101010.jpg").write_bytes(data + n.to_bytes(4, "big"))  # different bytes
    start = time.perf_counter()
    _, p = build(folder, prefs=prefs_mod.Prefs(dedupe=False))
    elapsed = time.perf_counter() - start
    names = {os.path.basename(i.dst) for i in p.items}
    assert len(names) == 5000 and "IMG_20240101_101010 (5000).jpg" in names
    assert elapsed < 20, elapsed


def test_already_organized_folder_has_nothing_to_do(sample_folder):
    _, p = build(sample_folder, prefs=CONVERT)
    mover.execute(p)
    _, again = build(sample_folder, prefs=CONVERT)
    assert again.actionable() == 0
    assert {i.status for i in again.items} <= {plan_mod.SAME, plan_mod.NODATE}


# ------------------------------------------------------------------ log
def test_broken_log_is_kept_aside(tmp_path):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "IMG_20240101_101010.jpg"))
    (folder / "organize_log.json").write_text("{not json", encoding="utf-8")
    _, p = build(folder)
    res = mover.execute(p)
    assert not res.failed
    kept = [n for n in os.listdir(folder) if n.startswith("organize_log.json.broken-")]
    assert len(kept) == 1 and open(folder / kept[0], encoding="utf-8").read() == "{not json"


def test_log_that_cannot_be_written_moves_nothing(tmp_path, monkeypatch):
    folder = tmp_path / "in1"
    ms.photo(str(folder / "IMG_20240101_101010.jpg"))
    before = snapshot(folder)
    _, p = build(folder)

    def fail(path, data):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(mover, "save_log", fail)
    with pytest.raises(OSError):
        mover.execute(p)
    assert snapshot(folder) == before


def test_log_failing_mid_run_stops_and_keeps_the_moved_list(tmp_path, monkeypatch):
    folder = tmp_path / "in1"
    for n in range(6):
        ms.photo(str(folder / f"IMG_2024010{n + 1}_101010.jpg"), seed=n)
    _, p = build(folder)
    real_write = mover.RunLog._write
    count = {"n": 0}

    def write(self, obj):
        count["n"] += 1
        if count["n"] > 6:  # header + a few steps fit, then the disk is full
            raise mover.LogWriteError(errno.ENOSPC, "disk full", "journal")
        return real_write(self, obj)

    monkeypatch.setattr(mover.RunLog, "_write", write)
    monkeypatch.setattr(mover, "save_log", lambda path, data: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full"))
                        if count["n"] > 6 else None)
    res = mover.execute(p)
    assert res.cancelled and res.done < 6
    assert res.moved_paths and len(res.moved_paths) == res.done
    assert any(r == prefs_mod.i18n.t("err_log_lost") for _, r in res.failed)


def test_log_failing_mid_run_but_saved_at_the_end_can_be_undone(tmp_path, monkeypatch):
    folder = tmp_path / "in1"
    for n in range(6):
        ms.photo(str(folder / f"IMG_2024010{n + 1}_101010.jpg"), seed=n)
    before = snapshot(folder)
    _, p = build(folder)
    real_write = mover.RunLog._write
    count = {"n": 0}

    def write(self, obj):
        count["n"] += 1
        if count["n"] > 6:
            raise mover.LogWriteError(errno.ENOSPC, "disk full", "journal")
        return real_write(self, obj)

    monkeypatch.setattr(mover.RunLog, "_write", write)
    res = mover.execute(p)
    assert res.cancelled and any(r == prefs_mod.i18n.t("err_log_stopped") for _, r in res.failed)
    monkeypatch.setattr(mover.RunLog, "_write", real_write)
    u = undo.undo(undo.find_log(str(folder)))
    assert not u.skipped
    assert without_log(snapshot(folder)) == without_log(before)


# ------------------------------------------------------------------ undo round trip (완료 기준)
def test_thousand_photos_organize_convert_and_undo_back_to_identical(tmp_path):
    folder = tmp_path / "in1"
    small = (48, 36)
    for n in range(940):
        day = 1 + n % 28
        sub = folder / f"dump{n % 7}"
        # every name carries n: one name per file (a fixture that overwrote its own files once gave 802 of 1010)
        if n % 5 == 0:
            ms.photo(str(sub / f"KakaoTalk_202403{day:02d}_1010{n % 60:02d}{n:03d}.jpg"), size=small, seed=n)
        elif n % 5 == 1:
            ms.photo(str(sub / f"IMG_{n:04d}.jpg"), exif=ms.exif_bytes(f"2023:0{1 + n % 9}:{day:02d} 10:00:00"),
                     size=small, seed=n)
        elif n % 5 == 2:
            ms.photo(str(sub / f"Screenshot_202402{day:02d}_10{n % 60:02d}00_{n}.png"), "PNG", size=small, seed=n)
        elif n % 5 == 3:
            ms.photo(str(sub / f"photo_{n}.jpg"), size=small, seed=n)  # no date: stays
        else:
            ms.photo(str(sub / f"2022{1 + n % 12:02d}{day:02d}_101010({n}).jpg"), size=small, seed=n)
    for n in range(30):  # HEIC with live photos: converted, then the JPGs must go on undo
        ms.photo(str(folder / "iphone" / f"IMG_9{n:03d}.HEIC"), "HEIF", ms.exif_bytes("2024:07:07 07:07:07"),
                 size=small, seed=5000 + n)
        ms.video(str(folder / "iphone" / f"IMG_9{n:03d}.MOV"), local="2024-07-07T07:07:07+0900")
    for n, original in enumerate(sorted((folder / "dump1").glob("IMG_*.jpg"))[:10]):  # exact duplicates
        shutil.copy2(original, folder / "dump3" / f"copy_{n}.jpg")
    before = snapshot(folder)
    assert len(before["files"]) == 1010
    _, p = build(folder, prefs=CONVERT)
    s = p.summary()
    assert s["convert"] == 30 and s["dupes"] == 10 and s["move"] > 600
    res = mover.execute(p)
    assert not res.failed and res.converted == 30
    u = undo.undo(undo.find_log(str(folder)))
    assert not u.skipped and not u.log_unsaved
    assert without_log(snapshot(folder)) == without_log(before)


# ------------------------------------------------------------------ CLI
def run_cli(*args, cwd=None):
    settings = os.path.join(str(cwd), "cli-settings.json")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PHOTO_ORGANIZER_SETTINGS=settings)
    return subprocess.run([sys.executable, os.path.join(ROOT, "main.py"), *args], capture_output=True, text=True,
                          encoding="utf-8", stdin=subprocess.DEVNULL, env=env, timeout=120, cwd=cwd)


def test_cli_dry_run_changes_nothing(sample_folder, tmp_path):
    before = snapshot(sample_folder)
    out = run_cli(str(sample_folder), "--dry-run", "--convert-heic", "--lang", "en", cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert "Dry run" in out.stdout and "IMG_0001.jpg" in out.stdout
    assert snapshot(sample_folder) == before


def test_cli_run_and_undo(sample_folder, tmp_path):
    before = snapshot(sample_folder)
    out = run_cli(str(sample_folder), "--convert-heic", "--lang", "ko", cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert "완료" in out.stdout
    assert os.path.exists(sample_folder / "2024" / "2024-03-15" / "IMG_0001.jpg")
    back = run_cli(str(sample_folder), "--undo", "--lang", "en", cwd=str(tmp_path))
    assert back.returncode == 0, back.stderr
    assert without_log(snapshot(sample_folder)) == without_log(before)
    again = run_cli(str(sample_folder), "--undo", "--lang", "en", cwd=str(tmp_path))
    assert again.returncode == 1 and "Nothing to undo" in again.stdout


def test_cli_heic_only_converts_in_place(sample_folder, tmp_path):
    out = run_cli(str(sample_folder), "--heic-only", cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert os.path.exists(sample_folder / "IMG_0001.jpg") and os.path.exists(sample_folder / "IMG_0001.HEIC")
    assert not os.path.exists(sample_folder / "2024")


def test_cli_rejects_bad_pattern_and_copy_in_place(sample_folder, tmp_path):
    assert run_cli(str(sample_folder), "--pattern", "{nope}", cwd=str(tmp_path)).returncode == 2
    assert run_cli(str(sample_folder), "--copy", cwd=str(tmp_path)).returncode == 2


def test_cli_does_not_leave_settings_in_the_repo(sample_folder, tmp_path):
    """organizer D7: running main.py from the repo used to write settings.json (with last_log) there."""
    repo_settings = os.path.join(ROOT, "settings.json")
    existed = os.path.exists(repo_settings)
    run_cli(str(sample_folder), cwd=str(tmp_path))
    assert os.path.exists(repo_settings) == existed
    data = json.load(open(tmp_path / "cli-settings.json", encoding="utf-8"))
    assert data["last_log"] == str(sample_folder / "organize_log.json")


def test_dates_module_is_pure_after_reading(sample_folder, monkeypatch):
    """Changing a date option re-plans without opening any file again."""
    res = scan.scan(str(sample_folder))
    monkeypatch.setattr(dates, "read_meta", lambda *a: (_ for _ in ()).throw(AssertionError("reopened")))
    opts = prefs_mod.options(prefs_mod.Prefs(use_mtime=True), str(sample_folder))
    p = plan_mod.build(res, opts)
    assert by_name(p, "IMG_1234.jpg").basis == dates.B_MTIME
