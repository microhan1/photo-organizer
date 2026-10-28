"""The built exe opens its window and closes again, cut off from the internet.

Opt-in: set PHOTO_ORGANIZER_EXE to the built exe (dist\\photo-organizer.exe); a normal
test run does not launch it. Never run it together with the GUI tests. The exe is copied
to a temporary folder (settings.json is written beside the exe), APPDATA points at an
empty temporary folder, and HTTP(S)_PROXY point at a dead port, so the start-up update
check cannot reach the 책갈피 server. The window is found by its process and closed with
WM_CLOSE, as its own close button would; nothing is clicked, "Send feedback" is not
touched, and no keyboard focus is taken."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

EXE = os.environ.get("PHOTO_ORGANIZER_EXE", "")
pytestmark = pytest.mark.skipif(sys.platform != "win32" or not os.path.isfile(EXE),
                                reason="set PHOTO_ORGANIZER_EXE to the built exe to run this")
DEAD_PROXY = "http://127.0.0.1:9"
WM_CLOSE = 0x0010


def tree(pid: int) -> set[int]:
    """The process and its children (the one-file exe runs the program in a child)."""
    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    pairs, entry = [], Entry()
    entry.dwSize = ctypes.sizeof(Entry)
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        pairs.append((entry.th32ParentProcessID, entry.th32ProcessID))
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    found, grew = {pid}, True
    while grew:
        grew = False
        for parent, child in pairs:
            if parent in found and child not in found:
                found.add(child)
                grew = True
    return found


def tk_windows(pids: set[int]) -> list[tuple[int, str]]:
    """Visible top-level Tk windows of these processes, by handle."""
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def each(hwnd, _):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 256)
            if cls.value == "TkTopLevel":
                found.append((hwnd, title.value))
        return True

    user32.EnumWindows(each, 0)
    return found


def test_exe_opens_and_closes_offline(tmp_path):
    exe = tmp_path / "app" / os.path.basename(EXE)
    exe.parent.mkdir()
    shutil.copy2(EXE, exe)
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    env = dict(os.environ, HTTP_PROXY=DEAD_PROXY, HTTPS_PROXY=DEAD_PROXY, http_proxy=DEAD_PROXY,
               https_proxy=DEAD_PROXY, NO_PROXY="", no_proxy="", APPDATA=str(appdata),
               LOCALAPPDATA=str(appdata))
    env.pop("PHOTO_ORGANIZER_SETTINGS", None)
    proc = subprocess.Popen([str(exe)], env=env, cwd=str(tmp_path))
    try:
        end, found = time.time() + 90, []
        while time.time() < end and not found:
            time.sleep(0.5)
            assert proc.poll() is None, f"exe exited early with {proc.returncode}"
            found = tk_windows(tree(proc.pid))
        assert found, "no window appeared"
        hwnd, title = found[0]
        assert title in ("사진 정리", "Photo Organizer", "照片整理", "写真整理"), title
        time.sleep(4)  # past the 1.5 s update check, which must fail quietly behind the dead proxy
        assert proc.poll() is None, "the exe died while the update check was out"
        assert tk_windows(tree(proc.pid)), "the window went away by itself"
        ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0
    settings = exe.parent / "settings.json"
    if settings.exists():
        data = json.loads(settings.read_text(encoding="utf-8"))
        assert not data.get("update_check_last"), "the server answered through a dead proxy?"
    assert not (appdata / "photo-organizer").exists()  # the folder beside the exe was writable
