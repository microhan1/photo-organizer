"""Walk a folder and read what each photo / video says about its date. Read only.

Cloud-only placeholders (OneDrive, iCloud) are recognised from the directory
listing alone and never opened: opening one starts a download.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import functools
import hashlib
import os
import stat as stat_mod
import sys
import threading
import unicodedata
from typing import Callable

import dates
from longpath import fs, plain  # noqa: F401  (plain: re-exported)

LOG_NAME = "organize_log.json"
JUNK_NAMES = {"thumbs.db", ".ds_store", "desktop.ini", "ehthumbs.db"}

HEIC_EXTS = {".heic", ".heif", ".hif"}
AVIF_EXTS = {".avif"}
JPEG_EXTS = {".jpg", ".jpeg", ".jpe", ".jfif"}
RAW_EXTS = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf", ".pef", ".srw"}
OTHER_IMAGE_EXTS = {".png", ".gif", ".webp", ".tif", ".tiff", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".3gp", ".m4v"}
SIDECAR_EXTS = {".aae", ".xmp"}
MEDIA_EXTS = HEIC_EXTS | AVIF_EXTS | JPEG_EXTS | RAW_EXTS | OTHER_IMAGE_EXTS | VIDEO_EXTS | SIDECAR_EXTS

# Windows attributes of files whose data is not on this computer
CLOUD_ATTRS = 0x00400000 | 0x00040000 | 0x00001000  # RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE
MAGIC_BYTES = 32
READ_THREADS = 8  # files read at the same time during a scan
READ_CHUNK = 256  # files per step between cancel checks and progress reports
HEAD_BYTES = 64 * 1024  # read once per file: format, quick hash (EXIF is read through the same handle)

ProgressFn = Callable[[int, int], None]


def is_junk(name: str) -> bool:
    """System clutter that never counts as content: Thumbs.db, desktop.ini, .DS_Store, ._* (macOS)."""
    low = name.lower()
    return low in JUNK_NAMES or low.startswith("._")


def key_of(path: str) -> str:
    """Identity of an existing path: case-insensitive like Windows. NTFS keeps NFC
    and NFD spellings apart, so two such files stay two keys."""
    return os.path.normcase(os.path.abspath(path))


def target_key(path: str) -> str:
    """Key for new names: NFC too, so the tool never creates two names that only
    differ in Unicode normalization (they look identical in Explorer)."""
    return os.path.normcase(unicodedata.normalize("NFC", os.path.abspath(path)))


def stem_key(name: str) -> str:
    """Pairs match on the name without extension, case- and normalization-insensitive."""
    return unicodedata.normalize("NFC", os.path.splitext(name)[0]).casefold()


def kind_of_ext(ext: str) -> str:
    ext = ext.lower()
    if ext in HEIC_EXTS:
        return "heic"
    if ext in AVIF_EXTS:
        return "avif"
    if ext in JPEG_EXTS:
        return "jpeg"
    if ext in RAW_EXTS:
        return "raw"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in SIDECAR_EXTS:
        return "sidecar"
    return "image"


def sniff(head: bytes) -> str:
    """Real format from the first bytes ("" when unknown). The extension is never changed."""
    if head[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    if head[:2] == b"BM":
        return "BMP"
    if head[:4] in (b"II*\x00", b"MM\x00*", b"IIRO", b"IIU\x00"):
        return "TIFF"  # also DNG, CR2, NEF, ARW, ORF, RW2
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1", b"mif2"):
            return "HEIC"
        if brand in (b"avif", b"avis"):
            return "AVIF"
        if brand == b"qt  ":
            return "MOV"
        if brand.startswith(b"3g"):
            return "3GP"
        if brand == b"crx ":
            return "CR3"
        return "MP4"
    if head[4:8] in (b"moov", b"wide", b"mdat", b"free", b"skip"):
        return "MOV"  # old QuickTime without ftyp
    return ""


@dataclasses.dataclass
class Media:
    path: str
    size: int
    mtime: float
    kind: str  # heic, avif, jpeg, raw, image, video, sidecar (by extension)
    fmt: str = ""  # real format by magic bytes
    meta: dates.Meta = dataclasses.field(default_factory=dates.Meta)
    readonly: bool = False
    quick: str = ""  # SHA-1 of the first HEAD_BYTES: files that differ there are not duplicates
    # filled in when needed (dedupe), kept while the file is unchanged
    sha1: str = dataclasses.field(default="", compare=False, repr=False)
    phash: tuple | None = dataclasses.field(default=None, compare=False, repr=False)
    dims: tuple[int, int] | None = dataclasses.field(default=None, compare=False, repr=False)

    # the path never changes on a Media (a moved file is a new Media), so these are computed once
    @functools.cached_property
    def name(self) -> str:
        return os.path.basename(self.path)

    @functools.cached_property
    def folder(self) -> str:
        return os.path.dirname(self.path)

    @functools.cached_property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0]

    @functools.cached_property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1]

    @functools.cached_property
    def key(self) -> str:
        return key_of(self.path)

    @property
    def is_heif(self) -> bool:
        """Convertible HEIF: by the bytes, or by the name when the bytes say nothing."""
        return self.fmt == "HEIC" or (not self.fmt and self.kind == "heic")

    @property
    def is_avif(self) -> bool:
        return self.fmt == "AVIF" or (not self.fmt and self.kind == "avif")


@dataclasses.dataclass
class DirInfo:
    path: str
    files: list[str]  # every file name, media or not
    subdirs: list[str]


@dataclasses.dataclass
class ScanResult:
    root: str
    media: list[Media]
    dirs: dict[str, DirInfo]  # key_of(path) -> DirInfo
    cloud: list[str] = dataclasses.field(default_factory=list)  # cloud-only files, skipped unopened
    denied: list[str] = dataclasses.field(default_factory=list)  # folders that could not be listed
    total_bytes: int = 0
    cancelled: bool = False


class MetaCache:
    """Metadata read by an earlier scan, so a rescan (after a run or an undo) does
    not open unchanged files again. Keyed by volume + file number where the drive
    has them (a move inside a drive keeps them), else by path; valid only while
    size and modified time are the same. Memory only."""

    def __init__(self) -> None:
        self._entries: dict[tuple, Media] = {}
        self.hits = 0

    @staticmethod
    def key(path: str, st: os.stat_result) -> tuple:
        return ("id", st.st_dev, st.st_ino) if st.st_ino else ("path", key_of(path))

    def get(self, path: str, st: os.stat_result) -> Media | None:
        old = self._entries.get(self.key(path, st))
        if old is None or old.size != st.st_size or old.mtime != st.st_mtime:
            return None
        return dataclasses.replace(old, path=os.path.abspath(path), readonly=_readonly(st))

    def add(self, media: Media, st: os.stat_result) -> None:
        if not media.meta.error:
            self._entries[self.key(media.path, st)] = media


def _readonly(st: os.stat_result) -> bool:
    return not (st.st_mode & stat_mod.S_IWRITE)


def is_cloud_only(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & CLOUD_ATTRS)


def read_media(path: str, st: os.stat_result) -> Media:
    """One open per file: the first HEAD_BYTES give the real format and a quick hash for
    duplicate finding, and the same handle reads the EXIF / video boxes."""
    m = Media(os.path.abspath(path), st.st_size, st.st_mtime, kind_of_ext(os.path.splitext(path)[1]),
              readonly=_readonly(st))
    if m.size == 0 or m.kind == "sidecar":
        return m
    try:
        with open(fs(path), "rb") as f:
            head = f.read(HEAD_BYTES)
            m.fmt = sniff(head[:MAGIC_BYTES])
            m.quick = hashlib.sha1(head).hexdigest()
            if len(head) == m.size:
                m.sha1 = m.quick  # the whole file was read: this is its full hash
            if m.fmt in dates.OWN_READERS:
                m.meta = dates.read_meta(path, m.fmt, m.size, f)
                return m
    except OSError as exc:
        m.meta = dates.Meta(error=f"{exc.__class__.__name__}: {exc}"[:200])
        return m
    m.meta = dates.read_meta(path, m.fmt, m.size)  # HEIC, PNG, WebP: their libraries open the file
    return m


def scan(root: str, exclude: list[str] | None = None, progress: ProgressFn | None = None,
         cancel: threading.Event | None = None, cache: MetaCache | None = None) -> ScanResult:
    """Every photo, video and sidecar under root (sorted). ``exclude`` lists folders
    whose subtree is skipped (the duplicates folder, a destination inside root)."""
    root = os.path.abspath(root)
    skip = {key_of(p) for p in (exclude or [])}
    res = ScanResult(root, [], {})
    found: list[tuple[str, os.stat_result]] = []
    stack = [root]
    while stack:
        if cancel is not None and cancel.is_set():
            res.cancelled = True
            return res
        here = stack.pop()
        try:
            with os.scandir(fs(here)) as it:  # paths are built from `here`, never from the \\?\ form
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            res.denied.append(here)
            continue
        files, subdirs = [], []
        for e in entries:
            path = os.path.join(here, e.name)
            try:
                if e.is_dir(follow_symlinks=False):
                    if key_of(path) not in skip:
                        subdirs.append(e.name)
                    continue
                st = e.stat(follow_symlinks=False)  # from the listing on Windows: does not open the file
            except OSError:
                continue
            files.append(e.name)
            if is_junk(e.name) or os.path.splitext(e.name)[1].lower() not in MEDIA_EXTS:
                continue
            if is_cloud_only(st):
                res.cloud.append(path)
                continue
            found.append((path, st))
        res.dirs[key_of(here)] = DirInfo(here, files, subdirs)
        stack.extend(os.path.join(here, d) for d in reversed(subdirs))
    found.sort(key=lambda x: x[0])
    total = len(found)

    def one(item: tuple[str, os.stat_result]) -> tuple[Media, bool]:
        """(media, taken from the cache)."""
        path, st = item
        m = cache.get(path, st) if cache is not None else None
        return (m, True) if m is not None else (read_media(path, st), False)

    # Reading is mostly waiting (the disk, the virus scanner on a first open): a few threads
    # overlap the waits. A 100,000-file folder opened for the first time took 124 s in one thread.
    # Results come back in order, in chunks, so cancel and progress stay prompt.
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=READ_THREADS) as pool:
        for start in range(0, total, READ_CHUNK):
            if cancel is not None and cancel.is_set():
                res.cancelled = True
                return res
            chunk = found[start:start + READ_CHUNK]
            for (path, st), (m, hit) in zip(chunk, pool.map(one, chunk)):
                if cache is not None:  # the cache is only touched here, on the calling thread
                    if hit:
                        cache.hits += 1
                    else:
                        cache.add(m, st)
                res.media.append(m)
                res.total_bytes += m.size
            done += len(chunk)
            if progress is not None:
                progress(done, total)
    return res


def network_path(path: str) -> bool:
    """A network drive or UNC share (slow: the GUI warns, hashing shows an estimate)."""
    p = os.path.abspath(path)
    if p.startswith("\\\\"):
        return True
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return ctypes.windll.kernel32.GetDriveTypeW(os.path.splitdrive(p)[0] + "\\") == 4  # DRIVE_REMOTE
    except Exception:
        return False
