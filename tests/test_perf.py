"""PRD "성능 관련". The 100,000-file check of the 완료 기준 takes minutes and ~1 GB of
disk: it runs only with PHOTO_PERF_100K=1 (python -m pytest tests/test_perf.py -k 100k)."""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time

import pytest
from conftest import ROOT, build, ms

import dedupe
import mover
import prefs as prefs_mod
import scan

SCAN_100K_SECONDS = 120
MEMORY_LIMIT_MB = 500


def fill(folder, count: int, heic_every: int = 0) -> None:
    """``count`` small dated JPEGs in folders of 500 (different bytes each), some HEIC."""
    jpg = open(ms.photo(str(folder.parent / "t.jpg"), exif=ms.exif_bytes("2024:03:15 12:34:56"), size=(32, 24)),
               "rb").read()
    heic = open(ms.photo(str(folder.parent / "t.heic"), "HEIF", ms.exif_bytes("2024:03:15 12:34:56"), size=(32, 24)),
                "rb").read() if heic_every else b""
    for n in range(count):
        d = folder / f"d{n // 500:03d}"
        if n % 500 == 0:
            os.makedirs(d, exist_ok=True)
        if heic_every and n % heic_every == 0:
            (d / f"IMG_{n:06d}.HEIC").write_bytes(heic)
        else:
            (d / f"IMG_{n:06d}.jpg").write_bytes(jpg + n.to_bytes(4, "big"))


def peak_mb() -> float:
    class PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t), ("a", ctypes.c_size_t), ("b", ctypes.c_size_t),
                    ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = wintypes.HANDLE  # without this the handle was cut and the call failed (0 MB)
    k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    if not k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
        raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
    return pmc.PeakWorkingSetSize / 1024 / 1024


def test_ten_thousand_files_scan_and_plan(tmp_path):
    folder = tmp_path / "in1"
    fill(folder, 10_000)
    start = time.perf_counter()
    res, p = build(folder)
    elapsed = time.perf_counter() - start
    assert len(res.media) == 10_000 and p.summary()["move"] == 10_000
    assert elapsed < 30, elapsed  # 100k must stay within two minutes


def test_scan_cancel_stops_within_two_seconds(tmp_path):
    folder = tmp_path / "in1"
    fill(folder, 6_000)
    cancel = threading.Event()
    out = {}

    def run():
        out["res"] = scan.scan(str(folder), cancel=cancel, progress=lambda d, t: cancel.set() if d > 100 else None)

    start = time.perf_counter()
    t = threading.Thread(target=run)
    t.start()
    t.join(10)
    assert out["res"].cancelled and time.perf_counter() - start < 2


def test_run_cancel_stops_within_two_seconds_and_can_be_undone(tmp_path):
    folder = tmp_path / "in1"
    fill(folder, 400, heic_every=4)  # 100 HEIC conversions run in worker processes
    _, p = build(folder, prefs=prefs_mod.Prefs(convert=True))
    cancel = threading.Event()
    pressed = {}

    def progress(done, total):
        if done == 300 and not cancel.is_set():  # in the conversion phase
            pressed["at"] = time.perf_counter()
            cancel.set()

    res = mover.execute(p, progress=progress, cancel=cancel)
    stopped = time.perf_counter() - pressed["at"]
    assert res.cancelled and stopped < 2, stopped
    leftovers = [f for here, _, fs in os.walk(folder) for f in fs if f.endswith(".part")]
    assert leftovers == []  # half-written JPGs are cleaned up
    import undo

    u = undo.undo(undo.find_log(str(folder)))
    assert not u.skipped


def test_similar_estimate_is_shown_before_and_is_honest(tmp_path):
    folder = tmp_path / "in1"
    for n in range(40):
        ms.photo(str(folder / f"IMG_{n:04d}.jpg"), size=(1200, 900), seed=n)
    _, p = build(folder)
    estimate = dedupe.estimate_seconds(p.items)
    start = time.perf_counter()
    dedupe.find_similar(p.items)
    real = time.perf_counter() - start
    assert real < max(3 * estimate, 1.0), (real, estimate)


def test_memory_for_ten_thousand_files_in_a_fresh_process(tmp_path):
    folder = tmp_path / "in1"
    fill(folder, 10_000)
    code = (f"import sys; sys.path.insert(0, {ROOT!r}); sys.path.insert(0, {os.path.join(ROOT, 'tests')!r});"
            "import json, test_perf, conftest; r, p = conftest.build(sys.argv[1]);"
            "print(json.dumps({'n': len(r.media), 'mb': test_perf.peak_mb()}))")
    out = subprocess.run([sys.executable, "-c", code, str(folder)], capture_output=True, text=True, timeout=300,
                         env=dict(os.environ, PHOTO_ORGANIZER_SETTINGS=str(tmp_path / "s.json")),
                         stdin=subprocess.DEVNULL)
    data = json.loads(out.stdout.strip().splitlines()[-1])
    assert data["n"] == 10_000 and 10 < data["mb"] < MEMORY_LIMIT_MB / 2, data  # 0 MB = a broken meter


@pytest.mark.skipif(os.environ.get("PHOTO_PERF_100K") != "1", reason="set PHOTO_PERF_100K=1 (minutes, ~1 GB)")
def test_100k_files_scan_within_two_minutes_and_500_mb(tmp_path):
    folder = tmp_path / "in1"
    fill(folder, 100_000, heic_every=20)
    code = (f"import sys, time; sys.path.insert(0, {ROOT!r}); sys.path.insert(0, {os.path.join(ROOT, 'tests')!r});"
            "import json, test_perf, conftest; t = time.perf_counter(); r, p = conftest.build(sys.argv[1]);"
            "print(json.dumps({'n': len(r.media), 's': time.perf_counter() - t, 'mb': test_perf.peak_mb()}))")
    out = subprocess.run([sys.executable, "-c", code, str(folder)], capture_output=True, text=True, timeout=900,
                         env=dict(os.environ, PHOTO_ORGANIZER_SETTINGS=str(tmp_path / "s.json")),
                         stdin=subprocess.DEVNULL)
    data = json.loads(out.stdout.strip().splitlines()[-1])
    print(data)
    assert data["n"] == 100_000
    assert data["s"] < SCAN_100K_SECONDS and 10 < data["mb"] < MEMORY_LIMIT_MB, data
