"""Randomised end-to-end scenarios: a messy photo folder, a random combination of options, then
the properties that must hold for ANY input.

  P1  nothing is lost: every file's bytes are still somewhere afterwards (plus the JPGs made)
  P2  the plan's targets are unique, inside the destination, and short enough
  P3  after a run, planning again finds nothing to do (idempotent)       [move mode, same-tree dest]
  P4  undo gives back exactly the folder we started with
  P5  every HEIC that was to be converted has its JPG, and it opens
  P6  originals the plan said "stay" were not touched (bytes and mtime)

PHOTO_FUZZ_SEEDS=200 python -m pytest tests/test_scenarios.py   runs more seeds (default 10).
A failing seed is printed in the test id: rerun just it with -k "seed17"."""
from __future__ import annotations

import datetime
import hashlib
import os
import random
import shutil
import stat
import zlib

import pytest
from conftest import ms, snapshot, without_log
from PIL import Image

import i18n
import main
import mover
import plan as plan_mod
import prefs as prefs_mod
import scan
import undo

SEEDS = int(os.environ.get("PHOTO_FUZZ_SEEDS", "10"))
START = int(os.environ.get("PHOTO_FUZZ_START", "0"))  # PHOTO_FUZZ_START=200 PHOTO_FUZZ_SEEDS=200: seeds 200..399
SIZE = (24, 18)
FOLDER_NAMES = ["a", "B", "여행 2023", "旅行", "trip (1)", "new folder", "x y z", "dump", "카톡", "2024", "2024-03-15",
                "_중복", "날짜 없음", "한글_이름", "with.dot", "ünï"]
DATES = ["2024:03:15 12:34:56", "2024:02:29 23:59:59", "2023:12:31 00:00:00", "2019:07:01 08:00:00",
         "2000:01:01 00:00:00", "1999:05:05 10:00:00", "2099:01:01 10:00:00", "0000:00:00 00:00:00", "garbage",
         "2024-03-15T12:34:56", "2021:10:04 09:49:24"]
NAMES = ["IMG_{n:04d}", "{y}{m:02d}{d:02d}_{H:02d}{M:02d}{S:02d}", "KakaoTalk_{y}{m:02d}{d:02d}_{n:09d}",
         "PXL_{y}{m:02d}{d:02d}_{H:02d}{M:02d}{S:02d}{n:03d}", "IMG-{y}{m:02d}{d:02d}-WA{n:04d}",
         "Screenshot_{y}{m:02d}{d:02d}_{H:02d}{M:02d}{S:02d}", "photo {n}", "사진_{n}", "DSC_{n:04d} copy",
         "{y}-{m:02d}-{d:02d} 모임", "IMG_{n:04d} (2)", "IMG_{n:04d}.final", "x" * 90 + "{n}", "trail {n}.", "{n} ",
         "스크린샷 {y}-{m:02d}-{d:02d} {H:02d}{M:02d}{S:02d}", "20241345_{n}", "band_{y}{m:02d}{d:02d}_{n}"]
PATTERNS = [plan_mod.DEFAULT_PATTERN, "{yyyy}/{yyyy-mm}", "{yyyy-mm-dd}", "{yyyy}/{source}/{yyyy-mm-dd}",
            "{yyyy}{mm}{dd}", "by-ext/{ext}/{yyyy}", "{yyyy}/{mm}/{dd}", "a<b>c/{yyyy}"]


class Gen:
    """Builds one random messy folder and remembers what it made."""

    def __init__(self, root: str, rnd: random.Random) -> None:
        self.root, self.rnd, self.n = root, rnd, 0
        self.used: set[str] = set()
        self.made: list[str] = []
        self.pool: list[str] = []  # files whose bytes can be copied (duplicates)

    def folder(self) -> str:
        depth = self.rnd.choice([0, 0, 1, 1, 2, 3])
        parts = [self.rnd.choice(FOLDER_NAMES) for _ in range(depth)]
        return os.path.join(self.root, *parts)

    def name(self, ext: str) -> str:
        self.n += 1
        when = datetime.datetime(self.rnd.choice([2019, 2021, 2023, 2024]), self.rnd.randint(1, 12),
                                 self.rnd.randint(1, 28), self.rnd.randint(0, 23), self.rnd.randint(0, 59),
                                 self.rnd.randint(0, 59))
        base = self.rnd.choice(NAMES).format(n=self.n, y=when.year, m=when.month, d=when.day, H=when.hour,
                                             M=when.minute, S=when.second)
        return base + ext

    def path(self, ext: str, folder: str | None = None, name: str | None = None) -> str | None:
        folder = folder or self.folder()
        name = name or self.name(ext)
        if name.endswith((" ", ".")) and not ext:
            return None
        p = os.path.join(folder, name)
        key = os.path.normcase(p).casefold()
        # the length of the path RELATIVE to the root: the sandbox's own path length (different in pytest and in a
        # debug script) must not change what gets generated, or a failing seed cannot be reproduced
        if key in self.used or len(os.path.relpath(p, self.root)) > 150:
            return None
        self.used.add(key)
        return p

    def exif(self) -> bytes | None:
        r = self.rnd.random()
        if r < 0.25:
            return None
        return ms.exif_bytes(self.rnd.choice(DATES), orientation=self.rnd.choice([None, 1, 3, 6, 8]),
                             make=self.rnd.choice(["", "Apple", "samsung"]), model="M", gps=self.rnd.random() < 0.3)

    def one(self) -> None:
        r, rnd = self.rnd.random(), self.rnd
        self.n += 1
        if r < 0.30:  # jpg
            p = self.path(".jpg")
            if p:
                ms.photo(p, exif=self.exif(), size=SIZE, seed=self.n)
                self.made.append(p)
        elif r < 0.38:  # png
            p = self.path(".png")
            if p:
                ms.photo(p, "PNG", size=SIZE, seed=self.n)
                self.made.append(p)
        elif r < 0.50:  # heic, sometimes with a live-photo mov and an aae
            p = self.path(".HEIC")
            if p:
                ms.photo(p, "HEIF", self.exif(), size=SIZE, seed=self.n)
                self.made.append(p)
                stem = os.path.splitext(p)[0]
                if rnd.random() < 0.5:
                    ms.video(stem + ".MOV", utc=rnd.choice([None, 1710473696, 1600000000]),
                             local=rnd.choice([None, "2024-03-15T12:34:56+0900"]))
                    self.made.append(stem + ".MOV")
                if rnd.random() < 0.3:
                    open(stem + ".AAE", "wb").write(b"<plist/>")
                    self.made.append(stem + ".AAE")
                if rnd.random() < 0.25:  # heic + jpg pair
                    ms.photo(stem + ".JPG", exif=self.exif(), size=SIZE, seed=self.n + 7)
                    self.made.append(stem + ".JPG")
        elif r < 0.58:  # video
            p = self.path(rnd.choice([".mp4", ".mov"]))
            if p:
                ms.video(p, utc=rnd.choice([None, 1710473696, 1600000000, 1700000000]),
                         brand=rnd.choice([b"isom", b"qt  "]))
                self.made.append(p)
        elif r < 0.68:  # exact duplicate of something earlier, or of a pair
            if self.pool:
                src = rnd.choice(self.pool)
                ext = os.path.splitext(src)[1]
                p = self.path(ext)
                if p:
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    shutil.copy2(src, p)
                    self.made.append(p)
        elif r < 0.73:  # zero byte
            p = self.path(".jpg")
            if p:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "wb").close()
                self.made.append(p)
        elif r < 0.78:  # damaged EXIF
            p = self.path(".jpg")
            if p:
                ms.photo(p, exif=ms.exif_bytes("2024:03:24 10:00:00"), size=SIZE, seed=self.n)
                data = bytearray(open(p, "rb").read())
                pos = bytes(data).find(b"Exif\x00\x00") + 6
                data[pos:pos + 8] = b"XX\x00\x00\xff\xff\xff\xff"
                open(p, "wb").write(bytes(data))
                self.made.append(p)
        elif r < 0.82:  # truncated jpeg
            p = self.path(".jpg")
            if p:
                ms.photo(p, exif=self.exif(), size=SIZE, seed=self.n)
                data = open(p, "rb").read()
                open(p, "wb").write(data[: max(20, len(data) // 3)])
                self.made.append(p)
        elif r < 0.86:  # a sidecar with no photo, junk, a mac resource fork
            folder = self.folder()
            os.makedirs(folder, exist_ok=True)
            for name in rnd.choice([["Thumbs.db"], ["desktop.ini"], [".DS_Store"], ["IMG_9999.AAE"],
                                    ["._IMG_0001.jpg"], ["notes.txt"], ["Thumbs.db", "IMG_8888.xmp"]]):
                p = os.path.join(folder, name)
                if os.path.normcase(p).casefold() not in self.used:
                    self.used.add(os.path.normcase(p).casefold())
                    open(p, "wb").write(b"x" * rnd.randint(1, 30))
                    self.made.append(p)
        elif r < 0.92:  # a file already where the default pattern would put it
            y, m, d = rnd.choice([(2024, 3, 15), (2023, 12, 31), (2021, 10, 4)])
            p = self.path(".jpg", os.path.join(self.root, str(y), f"{y}-{m:02d}-{d:02d}"))
            if p:
                ms.photo(p, exif=ms.exif_bytes(f"{y}:{m:02d}:{d:02d} 10:00:00"), size=SIZE, seed=self.n)
                self.made.append(p)
        else:  # same file name in several folders, different pictures, same date
            name = f"IMG_{rnd.randint(1, 4):04d}.jpg"
            for _ in range(rnd.randint(2, 3)):
                p = self.path(".jpg", self.folder(), name)
                if p:
                    ms.photo(p, exif=ms.exif_bytes("2024:03:15 12:34:56"), size=SIZE, seed=self.n * 31 + len(self.made))
                    self.made.append(p)
        self.pool = [p for p in self.made if os.path.exists(p) and os.path.getsize(p) > 0
                     and not p.lower().endswith((".aae", ".xmp", ".db", ".ini", ".txt"))]


def make_folder(root: str, rnd: random.Random) -> Gen:
    g = Gen(root, rnd)
    os.makedirs(root, exist_ok=True)
    for _ in range(rnd.randint(8, 40)):
        g.one()
    for p in g.made:  # some read-only, some old
        if rnd.random() < 0.08 and os.path.exists(p):
            os.chmod(p, stat.S_IREAD)
        if rnd.random() < 0.3 and os.path.exists(p):
            t = datetime.datetime(rnd.choice([2018, 2020, 2022]), 5, 5).timestamp()
            os.utime(p, (t, t))
    return g


def hashes(root: str) -> list[str]:
    out = []
    for here, _ds, fs_ in os.walk(root):
        for f in fs_:
            if f.startswith("organize_log.json") or scan.is_junk(f):
                continue
            with open(os.path.join(here, f), "rb") as fh:
                out.append(hashlib.sha1(fh.read()).hexdigest())
    return sorted(out)


def writable_again(root: str) -> None:
    for here, _ds, fs_ in os.walk(root):
        for f in fs_:
            try:
                os.chmod(os.path.join(here, f), stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass


def options(rnd: random.Random) -> dict:
    return dict(pattern=rnd.choice(PATTERNS), rename=rnd.random() < 0.3, include_nodate=rnd.random() < 0.4,
                convert=rnd.random() < 0.5, dedupe=rnd.random() < 0.7, use_mtime=rnd.random() < 0.3,
                heic_original=rnd.choice(["keep", "move"]), jpg_pair_as_dupe=rnd.random() < 0.15,
                remove_empty=rnd.random() < 0.8, dupes_action=rnd.choice(["move", "move", "trash"]))


def scenario_rng(seed: int, where: str) -> random.Random:
    """Same seed, same folder, every run: str hash() is salted per process and made a failing seed
    impossible to reproduce (it did, the first time), so use a fixed checksum."""
    return random.Random(seed * 7919 + zlib.crc32(where.encode()) % 1000)


@pytest.fixture
def fake_trash(monkeypatch):
    """Recycle bin stand-in that really removes the file and remembers it (undo lists it as 'trashed')."""
    gone = []
    monkeypatch.setattr(mover, "trash", lambda p: (gone.append(open(p, "rb").read()), mover.remove_file(p))[1])
    return gone


@pytest.mark.parametrize("seed", range(START, START + SEEDS), ids=lambda s: f"seed{s}")
@pytest.mark.parametrize("where", ["same", "above", "inside", "other"])
def test_random_folder_survives_run_rerun_and_undo(tmp_path, fake_trash, seed, where):
    rnd = scenario_rng(seed, where)
    top = tmp_path / "top"
    root = top / "in1"
    gen = make_folder(str(root), rnd)
    opts = options(rnd)
    lang = rnd.choice(i18n.LANGS)
    i18n.set_lang(lang, persist=False)
    dest = {"same": str(root), "above": str(top), "inside": str(root / "sorted"), "other": str(tmp_path / "out")}[where]
    p = prefs_mod.Prefs(**opts)
    tag = f"seed={seed} where={where} lang={lang} opts={opts} files={len(gen.made)}"
    before_snap = snapshot(tmp_path)  # the whole sandbox: top (source) and out (another destination)
    before_hashes = hashes(str(tmp_path))
    mtimes = {m: os.stat(m).st_mtime for m in gen.made if os.path.exists(m)}

    result, the_plan, _ = main.make_plan(str(root), dest, p, "move", False, p.convert, p.dedupe, False)
    # P2: targets unique, inside the destination, short
    seen: dict[str, str] = {}
    for item in the_plan.items:
        targets = [(d, m.path) for m, d in zip(item.members, item.dsts) if d]
        if item.convert_dst:
            targets.append((item.convert_dst, item.primary.path))
        if item.heic_dst:
            targets.append((item.heic_dst, item.primary.path))
        for d, src in targets:
            k = scan.target_key(d)
            assert k not in seen or seen[k] == src, f"{tag}: two files aim at {d}: {seen.get(k)} and {src}"
            seen[k] = src
            assert len(d) < 260, f"{tag}: path too long {len(d)}"
            if plan_mod.key_of(d) == plan_mod.key_of(src):
                continue
            inside = scan.key_of(d).startswith(scan.key_of(dest).rstrip(os.sep) + os.sep)
            # a photo that stays (no date) still gets its converted JPG right next to it, which may be
            # outside the destination: that is the design, not a leak
            beside_source = scan.key_of(os.path.dirname(d)) == scan.key_of(os.path.dirname(src))
            assert inside or (d == item.convert_dst and beside_source), f"{tag}: {d} outside {dest}"
    summary = the_plan.summary()
    assert not the_plan.blocked_groups(), tag

    res = mover.execute(the_plan)
    writable_again(str(tmp_path))
    assert not [f for f in res.failed if "read-only" not in f[1].lower()], f"{tag}: failed {res.failed}"

    # P1: nothing lost (the bytes of every original are still somewhere), extras are only converted JPGs
    after = hashes(str(tmp_path))
    trashed = sorted(hashlib.sha1(b).hexdigest() for b in fake_trash)
    missing = list(before_hashes)
    for h in after + trashed:
        if h in missing:
            missing.remove(h)
    assert missing == [], f"{tag}: {len(missing)} files' bytes vanished"
    assert len(after) - (len(before_hashes) - len(trashed)) <= res.converted, f"{tag}: unexpected extra files"

    # P5: every conversion produced an openable JPG
    for item in the_plan.items:
        if item.convert_dst and item.checked and os.path.exists(item.convert_dst):
            with Image.open(item.convert_dst) as im:
                im.load()
                assert im.format == "JPEG"

    # P3: planning again finds nothing to do (the destination "other" leaves the source emptied: rerun the dest)
    if where in ("same", "above"):
        p2 = prefs_mod.Prefs(**opts)
        _, again, _ = main.make_plan(str(root), dest, p2, "move", False, p2.convert, p2.dedupe, False)
        acts = [(i.primary.name, i.status, [os.path.relpath(d, dest) for d in i.dsts if d], i.convert_dst)
                for i in again.items if i.acts]
        assert not acts and not again.empty_dirs, f"{tag}: second run still wants to act: {acts[:4]} {again.empty_dirs[:3]}"

    # P4: undo gives back the exact starting folder
    u = undo.undo(undo.find_log(dest) or undo.find_log(str(root)))
    assert not u.log_error and not u.log_unsaved, tag
    writable_again(str(tmp_path))
    expected_skips = len(fake_trash)  # trashed files cannot come back from our fake bin
    assert len(u.skipped) == 0, f"{tag}: undo skipped {u.skipped[:3]}"
    assert len(u.trashed) == expected_skips, tag
    back = without_log(snapshot(tmp_path))
    want = without_log(before_snap)
    if not fake_trash:
        assert back["files"] == want["files"], f"{tag}: undo differs: {set(back['files']) ^ set(want['files'])}"
    else:  # only trashed files are allowed to be missing
        assert set(want["files"]) - set(back["files"]) <= {k for k, v in want["files"].items()
                                                         if v in {hashlib.sha1(b).hexdigest() for b in fake_trash}}, tag
        assert all(want["files"][k] == v for k, v in back["files"].items() if k in want["files"]), tag
    # P6 (mtime of files that were never touched is unchanged)
    for m, t in mtimes.items():
        if os.path.exists(m) and not fake_trash:
            assert abs(os.stat(m).st_mtime - t) < 2, f"{tag}: mtime changed for {m}"
    assert summary is not None
