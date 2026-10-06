"""Run a Plan: move / copy photos with their paired files, convert HEIC to JPG,
send duplicates away, remove emptied folders, and record every step in
organize_log.json so undo.py can reverse it.

The log part (RunLog, journal, LogWriteError, load_log/save_log) and the file
operations are shared with music-folder-organizer; keep the two in step.
File contents are never rewritten. A move within one drive is a rename; across
drives the copy is verified by SHA-1 before the original is removed.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import errno
import hashlib
import json
import os
import shutil
import stat
import sys
import threading
import time
import uuid
from typing import Callable

import i18n
from scan import LOG_NAME, fs, is_junk, key_of, plain  # noqa: F401  (plain: used by callers as mover.plain)

FLUSH_EVERY = 50  # journal lines between fsyncs
JOURNAL_SUFFIX = ".journal"
CHUNK = 1024 * 1024
POLL_SECONDS = 0.2  # how often a running conversion batch looks at the cancel button
MIN_POOL_JOBS = 3  # fewer conversions run in this process (starting workers costs ~1 s)
CONVERT_GROWTH = 2  # space estimate: a JPG may be twice the HEIC
ProgressFn = Callable[[int, int], None]


class CopyMismatch(OSError):
    pass


class LogWriteError(OSError):
    """The undo log could not be saved mid-run: the run has to stop, not skip one file."""


@dataclasses.dataclass
class Result:
    done: int = 0
    converted: int = 0
    failed: list[tuple[str, str]] = dataclasses.field(default_factory=list)  # (path, reason)
    notes: list[tuple[str, str]] = dataclasses.field(default_factory=list)  # (path, text): not failures
    trashed: int = 0
    folders_removed: int = 0
    bytes_in: int = 0  # HEIC bytes converted
    bytes_out: int = 0  # JPG bytes written
    log_path: str = ""
    cancelled: bool = False
    moved_paths: list[tuple[str, str]] = dataclasses.field(default_factory=list)  # shown when the log is lost


# ------------------------------------------------------------------ log
def log_path_for(folder: str) -> str:
    return os.path.join(folder, LOG_NAME)


def journal_path(log_path: str) -> str:
    return log_path + JOURNAL_SUFFIX


def _read_log(path: str) -> tuple[dict, bool]:
    """(data, broken): broken = the file exists, is not empty, and cannot be read as a log."""
    empty = {"version": 1, "runs": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return empty, False
    except (OSError, ValueError):
        return empty, _size(path) > 0
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        return empty, True
    return data, False


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _merge_journal(path: str, data: dict) -> None:
    """Steps of a run that never finished (the program stopped mid-run) are only in
    the journal: put them into that run. Only an open run takes them, so a journal
    left behind by a finished run can never bring back steps an undo removed."""
    try:
        with open(journal_path(path), "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    header, ops = None, []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            break  # a line cut short when the program stopped: the ones before it count
        if header is None:
            header = obj.get("journal") if isinstance(obj, dict) else None
            if not isinstance(header, dict) or not header.get("id"):
                return
        elif isinstance(obj, dict):
            ops.append(obj)
    if header is None:
        return
    run = next((r for r in data["runs"] if r.get("id") == header["id"]), None)
    if run is None:  # the log itself lost it (replaced, set aside as broken)
        run = {**header, "ops": []}
        data["runs"].append(run)
    if not run.get("open"):
        return
    if len(ops) > len(run.get("ops") or []):
        run["ops"] = ops
    run.pop("open", None)


def load_log(path: str) -> dict:
    data, _ = _read_log(path)
    _merge_journal(path, data)
    return data


def save_log(path: str, data: dict) -> None:
    """Every caller saves what load_log returned, journal steps included, so a
    journal is no longer needed once this succeeds."""
    tmp = path + ".part"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        _remove_quietly(tmp)  # a failed save leaves no .part behind
        raise
    _remove_quietly(journal_path(path))


class RunLog:
    """organize_log.json is saved before the first file moves: when it cannot be
    saved the run does not start, so nothing ever moves without a way back.

    During the run each step is appended to organize_log.json.journal (one JSON
    line, flushed at once) instead of rewriting the whole log; the steps go into
    organize_log.json when the run ends. If the program stops mid-run, the next
    load_log takes them from the journal."""

    def __init__(self, folder: str, source: str, mode: str) -> None:
        os.makedirs(folder, exist_ok=True)
        self.path = log_path_for(folder)
        self.existed = os.path.exists(self.path)
        self.data, broken = _read_log(self.path)
        if broken:
            # unreadable: keep it for the user instead of writing over it
            os.replace(self.path, f"{self.path}.broken-{time.strftime('%Y%m%d-%H%M%S')}")
            self.existed = False
        _merge_journal(self.path, self.data)  # an earlier run that was cut short
        self.run = {"id": uuid.uuid4().hex[:12], "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "source": source, "dest": folder, "mode": mode, "ops": [], "undone": False, "open": True}
        self.data["runs"].append(self.run)
        self._pending = 0
        self._journal = None
        save_log(self.path, self.data)  # raises: the caller stops before touching any file
        try:
            self._journal = open(journal_path(self.path), "w", encoding="utf-8")
            self._write({"journal": {k: v for k, v in self.run.items() if k != "ops"}})
        except OSError:
            self.discard()
            raise

    def _write(self, obj: dict) -> None:
        try:
            self._journal.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self._journal.flush()  # in the OS now: survives the program stopping
            self._pending += 1
            if self._pending >= FLUSH_EVERY:  # on the disk now: survives a power cut
                self._pending = 0
                os.fsync(self._journal.fileno())
        except (OSError, ValueError) as exc:  # ValueError: the journal was closed
            raise LogWriteError(getattr(exc, "errno", None) or errno.EIO, str(exc), journal_path(self.path)) from exc

    def add(self, **op) -> None:
        """Raises LogWriteError; the step stays in memory so flush() can still save it."""
        self.run["ops"].append(op)
        self._write(op)

    def _close_journal(self) -> None:
        if self._journal is not None:
            try:
                self._journal.close()
            except OSError:
                pass  # every line was flushed when written; the final save has them all anyway
            self._journal = None

    def flush(self) -> None:
        """End of the run: write every step into organize_log.json and drop the journal.
        Raises LogWriteError; the journal then stays and the next load_log merges it."""
        self._close_journal()
        self.run.pop("open", None)
        try:
            save_log(self.path, self.data)
        except OSError as exc:
            self.run["open"] = True
            raise LogWriteError(exc.errno, str(exc), self.path) from exc

    def discard(self) -> None:
        """Nothing happened: leave the log as it was before this run."""
        self._close_journal()
        self.data["runs"].remove(self.run)
        try:
            if self.existed:
                save_log(self.path, self.data)
            else:
                os.remove(self.path)
                _remove_quietly(journal_path(self.path))
        except OSError:
            pass  # at worst an empty open run remains, which history and undo skip


# ------------------------------------------------------------------ file ops
def _same_device_error(exc: OSError) -> bool:
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == 17


def copy_verified(src: str, dst: str) -> None:
    """Copy with SHA-1 on the way, re-read the copy and compare. A mismatch removes the copy."""
    h_src = hashlib.sha1()
    tmp = dst + ".part"
    try:
        with open(fs(src), "rb") as fi, open(fs(tmp), "wb") as fo:
            while chunk := fi.read(CHUNK):
                h_src.update(chunk)
                fo.write(chunk)
        shutil.copystat(fs(src), fs(tmp))
        h_dst = hashlib.sha1()
        with open(fs(tmp), "rb") as f:
            while chunk := f.read(CHUNK):
                h_dst.update(chunk)
        if h_src.digest() != h_dst.digest():
            raise CopyMismatch(errno.EIO, "copy differs from the original", dst)
        os.replace(fs(tmp), fs(dst))
    except BaseException:
        _remove_quietly(tmp)
        raise


def move_file(src: str, dst: str) -> None:
    if os.path.lexists(fs(dst)) and os.path.normcase(src) != os.path.normcase(dst):
        raise FileExistsError(errno.EEXIST, "target exists", dst)
    if os.path.normcase(src) == os.path.normcase(dst) and src != dst:
        # only the case of the name changes: Windows needs a detour through another name
        tmp = f"{dst}.{uuid.uuid4().hex[:8]}.tmp"
        os.rename(fs(src), fs(tmp))
        os.rename(fs(tmp), fs(dst))
        return
    try:
        os.rename(fs(src), fs(dst))
        return
    except OSError as exc:
        if not _same_device_error(exc):
            raise
    copy_verified(src, dst)
    try:
        remove_file(src)
    except OSError:
        _remove_quietly(dst)  # the original is in use: keep it, drop the copy
        raise


def remove_file(path: str) -> None:
    """os.remove that also takes read-only files (the attribute is restored on failure)."""
    try:
        os.remove(fs(path))
        return
    except PermissionError:
        mode = os.stat(fs(path)).st_mode
        if mode & stat.S_IWRITE:
            raise  # not read-only: in use
    os.chmod(fs(path), stat.S_IWRITE)
    try:
        os.remove(fs(path))
    except OSError:
        os.chmod(fs(path), mode)
        raise


def copy_file(src: str, dst: str) -> None:
    if os.path.lexists(fs(dst)):
        raise FileExistsError(errno.EEXIST, "target exists", dst)
    copy_verified(src, dst)


def _remove_quietly(path: str) -> None:
    try:
        os.chmod(fs(path), stat.S_IWRITE)
        os.remove(fs(path))
    except OSError:
        pass


def trash(path: str) -> None:
    """Send to the recycle bin. Raises OSError when that is not possible."""
    if sys.platform != "win32":
        try:
            from send2trash import send2trash  # type: ignore
        except ImportError as exc:
            raise OSError(errno.ENOTSUP, "recycle bin not available") from exc
        send2trash(path)
        return
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = os.path.abspath(path) + "\0"  # the field needs a double NUL; ctypes adds the second
    op.fFlags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted or os.path.lexists(fs(path)):
        raise OSError(errno.EIO, f"recycle bin refused ({rc})", path)


def make_dirs(folder: str, log: RunLog | None) -> None:
    """Create missing folders one level at a time so each one is logged."""
    missing = []
    d = os.path.abspath(folder)
    while not os.path.isdir(fs(d)):
        missing.append(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    for d in reversed(missing):
        os.mkdir(fs(d))
        if log is not None:
            log.add(op="mkdir", path=d)


def only_junk(folder: str) -> bool:
    try:
        with os.scandir(fs(folder)) as it:
            return all(e.is_file() and is_junk(e.name) for e in it)
    except OSError:
        return False


def remove_junk_folder(folder: str) -> bool:
    if not only_junk(folder):
        return False
    try:
        for name in os.listdir(fs(folder)):
            p = os.path.join(folder, name)
            os.chmod(fs(p), stat.S_IWRITE)
            os.remove(fs(p))
        os.rmdir(fs(folder))
        return True
    except OSError:
        return False


def reason_of(exc: BaseException) -> str:
    if isinstance(exc, FileExistsError):
        return i18n.t("err_target_exists")
    if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (5, 32, 33):
        return i18n.t("err_in_use")
    if isinstance(exc, CopyMismatch):
        return i18n.t("err_copy_mismatch")
    if isinstance(exc, FileNotFoundError):
        return i18n.t("err_missing")
    return str(exc) or exc.__class__.__name__


# ------------------------------------------------------------------ space
def _existing_parent(path: str) -> str:
    d = os.path.abspath(path)
    while not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return d


def space_check(plan) -> tuple[int, int]:
    """(bytes needed on the destination drive, bytes free there). Copies, moves to another
    drive and new JPGs need room; a move within the drive does not."""
    opts = plan.options
    dest_drive = os.path.splitdrive(os.path.abspath(opts.dest))[0].lower()
    need = 0
    for item in plan.items:
        if not item.acts:
            continue
        for m, d in zip(item.members, item.dsts):
            if not d or key_of(d) == m.key:
                continue
            if opts.mode == "copy" or os.path.splitdrive(m.path)[0].lower() != dest_drive:
                need += m.size
        if item.convert_dst:
            need += item.primary.size * CONVERT_GROWTH
    try:
        free = shutil.disk_usage(_existing_parent(opts.dest)).free
    except OSError:
        free = need
    return need, free


# ------------------------------------------------------------------ run
def drive_gone(opts) -> bool:
    """The source or destination folder itself has disappeared (drive unplugged, share lost)."""
    return not os.path.isdir(fs(opts.root)) or not os.path.isdir(fs(opts.dest))


def _extra(item) -> dict:
    return {"manual_date": item.when.strftime("%Y-%m-%dT%H:%M:%S")} if item.basis == "manual" and item.when else {}


def _move_group(item, mode: str, log: RunLog, res: Result) -> bool:
    """Move (or copy) every member of one item. When one member fails, the ones already
    moved go back (copies are removed), so a pair is never split. Raises LogWriteError."""
    made: list[tuple[str, str]] = []
    current = item.primary.path
    try:
        for m, dst in zip(item.members, item.dsts):
            current = m.path
            if not dst:
                trash(m.path)
                log.add(op="trash", src=m.path)
                res.trashed += 1
                continue
            if key_of(dst) == m.key and os.path.basename(dst) == m.name:
                continue
            if not os.path.lexists(fs(m.path)):  # gone since the preview: create no folders for it
                raise FileNotFoundError(errno.ENOENT, "missing", m.path)
            make_dirs(os.path.dirname(dst), log)
            if mode == "copy":
                copy_file(m.path, dst)
                made.append((m.path, dst))
                st = os.stat(fs(dst))
                log.add(op="copy", src=m.path, dst=dst, size=st.st_size, mtime_ns=st.st_mtime_ns, **_extra(item))
            else:
                move_file(m.path, dst)
                made.append((m.path, dst))
                log.add(op="move", src=m.path, dst=dst, **_extra(item))
            res.moved_paths.append((m.path, dst))
        return True
    except LogWriteError:
        raise
    except OSError as exc:
        res.failed.append((current, reason_of(exc)))
        for src, dst in reversed(made):
            try:
                if mode == "copy":
                    remove_file(dst)  # its "copy" step stays in the log; undo finds it gone and moves on
                else:
                    move_file(dst, src)
                    log.add(op="move", src=dst, dst=src)  # undo replays both steps: the file ends where it was
                res.moved_paths.remove((src, dst))
            except LogWriteError:
                raise
            except OSError as back:
                res.failed.append((dst, reason_of(back)))
        return False


def _finish_convert(item, src: str, outcome, log: RunLog, res: Result) -> None:
    jpg = item.convert_dst
    part = jpg + ".part"
    if not outcome.ok:
        res.failed.append((src, i18n.t("err_convert", error=outcome.error)))
        return
    if os.path.lexists(fs(jpg)):
        _remove_quietly(part)
        res.failed.append((jpg, i18n.t("err_target_exists")))
        return
    try:
        os.replace(fs(part), fs(jpg))
    except OSError as exc:
        _remove_quietly(part)
        res.failed.append((jpg, reason_of(exc)))
        return
    st = os.stat(fs(jpg))
    log.add(op="convert", src=src, dst=jpg, size=st.st_size, mtime_ns=st.st_mtime_ns)
    res.converted += 1
    res.bytes_in += outcome.in_size
    res.bytes_out += outcome.out_size
    if outcome.burst:
        res.notes.append((src, i18n.t("note_burst")))
    if outcome.rejected_tags:
        res.notes.append((src, i18n.t("note_tags_dropped", tags=", ".join(outcome.rejected_tags))))
    if item.heic_dst:
        try:
            make_dirs(os.path.dirname(item.heic_dst), log)
            move_file(src, item.heic_dst)
            log.add(op="move", src=src, dst=item.heic_dst)
        except LogWriteError:
            raise
        except OSError as exc:
            res.failed.append((src, reason_of(exc)))


def _pool_size() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


def run_conversions(jobs, quality: int, icc: str, cancel: threading.Event | None, on_done) -> bool:
    """Convert in worker processes (CPU count - 1); ``on_done(job, outcome)`` runs here,
    in this thread, one at a time. Returns False when cancelled. Within POLL_SECONDS of
    the cancel button no new file starts; the ones running finish and are cleaned up."""
    import convert

    if len(jobs) < MIN_POOL_JOBS:
        for job in jobs:
            if cancel is not None and cancel.is_set():
                return False
            on_done(job, convert.convert_file(job[1], job[2], quality, icc))
        return True
    pending_jobs = list(jobs)
    try:
        with concurrent.futures.ProcessPoolExecutor(max_workers=_pool_size()) as pool:
            futures = {pool.submit(convert.convert_file, j[1], j[2], quality, icc): j for j in jobs}
            waiting = set(futures)
            while waiting:
                done, waiting = concurrent.futures.wait(waiting, timeout=POLL_SECONDS,
                                                        return_when=concurrent.futures.FIRST_COMPLETED)
                for f in done:
                    job = futures[f]
                    pending_jobs.remove(job)
                    try:
                        outcome = f.result()
                    except concurrent.futures.CancelledError:
                        continue
                    on_done(job, outcome)
                if cancel is not None and cancel.is_set():
                    for f in waiting:
                        f.cancel()
                    pool.shutdown(wait=True, cancel_futures=True)
                    for job in pending_jobs:
                        _remove_quietly(job[2] + ".part")
                    return False
    except concurrent.futures.process.BrokenProcessPool:
        for job in list(pending_jobs):  # the workers could not start: do the rest here
            if cancel is not None and cancel.is_set():
                return False
            on_done(job, convert.convert_file(job[1], job[2], quality, icc))
    return True


def execute(plan, progress: ProgressFn | None = None, cancel: threading.Event | None = None,
            quality: int = 92, icc: str = "keep", on_file: Callable[[str], None] | None = None) -> Result:
    """``on_file(path)`` names the file being worked on (for the one-line status in the GUI)."""
    opts = plan.options
    items = [i for i in plan.items if i.acts]
    jobs_total = sum(1 for i in items if i.convert_dst)
    dirs = plan.empty_dirs if opts.remove_empty else []
    total = len(items) + jobs_total + len(dirs)
    res = Result()
    log = RunLog(opts.dest, opts.root, opts.mode)  # raises: nothing has been touched
    res.log_path = log.path
    log_stopped = False
    step = 0
    failed_items: set[int] = set()  # id() of items whose files could not move: no conversion either

    def tick() -> None:
        nonlocal step
        step += 1
        if progress is not None:
            progress(step, total)

    try:
        for item in items:
            if cancel is not None and cancel.is_set():
                res.cancelled = True
                break
            if on_file is not None:
                on_file(item.primary.path)
            try:
                if item.moves or item.trashes:
                    if _move_group(item, opts.mode, log, res):
                        res.done += 1
                    else:
                        failed_items.add(id(item))
                        if drive_gone(opts):  # an external drive was pulled: stop, the log has what moved
                            res.failed.append((opts.root, i18n.t("err_drive_gone")))
                            res.cancelled = True
                            break
            except LogWriteError:
                log_stopped = True
                res.cancelled = True
                break
            tick()
        if not res.cancelled and jobs_total:
            # (item, HEIC path after the move, JPG path)
            jobs = [(i, i.dsts[0] or i.primary.path, i.convert_dst) for i in items
                    if i.convert_dst and id(i) not in failed_items]

            def done(job, outcome) -> None:
                if on_file is not None:
                    on_file(job[2])
                _finish_convert(job[0], job[1], outcome, log, res)
                tick()

            try:
                if not run_conversions(jobs, quality, icc, cancel, done):
                    res.cancelled = True
            except LogWriteError:
                log_stopped = True
                res.cancelled = True
        if not res.cancelled:
            for folder in dirs:
                if cancel is not None and cancel.is_set():
                    res.cancelled = True
                    break
                if remove_junk_folder(folder):
                    res.folders_removed += 1
                    try:
                        log.add(op="rmdir", path=folder)
                    except LogWriteError:
                        log_stopped = True
                        res.cancelled = True
                        break
                tick()
    finally:
        if log.run["ops"]:
            try:
                log.flush()  # every step so far is in memory, even one the journal refused
                if log_stopped:
                    res.failed.append((log.path, i18n.t("err_log_stopped")))
            except LogWriteError:
                # the journal still holds every step it took; the next load merges them
                res.failed.append((log.path, i18n.t("err_log_lost" if log_stopped else "err_log_pending")))
            i18n.update_settings(last_log=log.path)
        else:
            log.discard()
            res.log_path = ""
    return res


def key_in(path: str, folder: str) -> bool:
    k, f = key_of(path), key_of(folder)
    return k == f or k.startswith(f.rstrip(os.sep) + os.sep)
