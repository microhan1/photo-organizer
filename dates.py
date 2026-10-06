"""When was a photo taken: EXIF, video metadata, file name, paired file, mtime.

Reading (``read_meta``) touches the file once and keeps every candidate; the
choice (``resolve``) is pure, so changing an option (modified time, year range,
a date typed in the table) never reopens files.

Priority (first that works wins):
  1 EXIF DateTimeOriginal   2 DateTimeDigitized / DateTime   3 video creation time
  4 file name               5 paired file (plan.py)           6 modified time (option)
EXIF times are local wall-clock times and are never shifted between time zones.
"""
from __future__ import annotations

import calendar
import dataclasses
import datetime
import io
import os
import struct

import patterns
from longpath import fs

# basis keys (lang: basis_<key>)
B_EXIF, B_EXIF_SUSPECT, B_VIDEO, B_NAME, B_NAME_MULTI, B_PAIR, B_MTIME, B_MANUAL = (
    "exif", "exif_suspect", "video", "name", "name_multi", "pair", "mtime", "manual")

# a camera whose clock was reset writes one of these
RESET_DATES = {datetime.datetime(1970, 1, 1), datetime.datetime(1980, 1, 1), datetime.datetime(2000, 1, 1)}
EXIF_FORMATS = ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S",
                "%Y:%m:%d %H:%M", "%Y-%m-%dT%H:%M", "%Y:%m:%d", "%Y-%m-%d")
JPEG_SCAN_LIMIT = 64 * 1024  # EXIF sits in the first segments; give up after this much
ATOM_READ_LIMIT = 1024 * 1024  # bytes of one metadata atom we are willing to read
MAC_EPOCH_OFFSET = 2082844800  # seconds from 1904-01-01 to 1970-01-01

TAG_ORIENTATION, TAG_MAKE, TAG_MODEL, TAG_DATETIME = 0x0112, 0x010F, 0x0110, 0x0132
TAG_EXIF_IFD, TAG_DT_ORIGINAL, TAG_DT_DIGITIZED = 0x8769, 0x9003, 0x9004
_WANTED_IFD0 = {TAG_ORIENTATION, TAG_MAKE, TAG_MODEL, TAG_DATETIME, TAG_EXIF_IFD}
_WANTED_EXIF = {TAG_DT_ORIGINAL, TAG_DT_DIGITIZED}
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


@dataclasses.dataclass(slots=True)  # one per file: no __dict__ saves memory on 100,000 files
class Meta:
    """Every date candidate one file holds."""
    exif_original: str = ""
    exif_digitized: str = ""
    exif_datetime: str = ""
    camera: str = ""  # Make Model, when the file says
    orientation: int = 0
    video_local: datetime.datetime | None = None  # QuickTime creationdate / (c)day: local wall-clock
    video_utc: datetime.datetime | None = None  # mvhd creation_time (UTC, as the format says)
    frames: int = 1  # images in a HEIF container (burst)
    error: str = ""  # the metadata could not be read (the file itself is still organized)


@dataclasses.dataclass
class DateOptions:
    use_mtime: bool = False
    year_min: int = 1990
    year_max: int = 0  # 0 = this year
    user_patterns: tuple[str, ...] = ()

    def years(self) -> tuple[int, int]:
        return self.year_min, self.year_max or datetime.date.today().year


@dataclasses.dataclass
class Resolved:
    when: datetime.datetime | None
    basis: str  # "" = no date
    source: str  # patterns.SOURCES


# ------------------------------------------------------------------ parsing
def parse_exif_datetime(text: str) -> datetime.datetime | None:
    """EXIF dates come as "2024:03:15 12:34:56" and, from some software, in other shapes.
    Zeros ("0000:00:00 00:00:00"), blanks and garbage give None."""
    if not isinstance(text, str):
        return None
    s = text.strip().strip("\x00").strip()
    if not s or s.startswith("0000"):
        return None
    s = s[:19]
    if len(s) == 19 and s[4] == s[7] == ":" and s[10] == " " and s[13] == s[16] == ":":
        # the standard EXIF shape, cut by hand: strptime is the slowest step of a 100,000-file scan
        try:
            return datetime.datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]), int(s[11:13]), int(s[14:16]),
                                     int(s[17:19]))
        except ValueError:
            return None
    for fmt in EXIF_FORMATS:
        try:
            return datetime.datetime.strptime(s[: len(datetime.datetime(2000, 1, 1).strftime(fmt))], fmt)
        except ValueError:
            continue
    return None


def _in_range(when: datetime.datetime | None, years: tuple[int, int]) -> bool:
    if when is None:
        return False
    if not years[0] <= when.year <= years[1]:
        return False
    return when <= datetime.datetime.now() + datetime.timedelta(days=1)  # the future is a wrong clock


# ------------------------------------------------------------------ TIFF / EXIF
def _read_ifd(f, base: int, offset: int, endian: str, wanted: set[int]) -> dict[int, object]:
    out: dict[int, object] = {}
    f.seek(base + offset)
    raw = f.read(2)
    if len(raw) < 2:
        return out
    (count,) = struct.unpack(endian + "H", raw)
    if count > 1000:
        return out
    entries = f.read(12 * count)
    for i in range(min(count, len(entries) // 12)):
        tag, typ, n, val = struct.unpack(endian + "HHI4s", entries[12 * i: 12 * i + 12])
        if tag not in wanted or typ not in _TYPE_SIZE:
            continue
        size = _TYPE_SIZE[typ] * n
        if size > 4:
            if size > 4096:
                continue
            here = f.tell()
            f.seek(base + struct.unpack(endian + "I", val)[0])
            data = f.read(size)
            f.seek(here)
        else:
            data = val[:size]
        if typ == 2:
            out[tag] = data.split(b"\x00", 1)[0].decode("ascii", "replace")
        elif typ == 3 and len(data) >= 2:
            out[tag] = struct.unpack(endian + "H", data[:2])[0]
        elif typ == 4 and len(data) >= 4:
            out[tag] = struct.unpack(endian + "I", data[:4])[0]
    return out


def read_tiff(f, base: int = 0) -> dict[int, object]:
    """IFD0 and the EXIF IFD of a TIFF structure starting at ``base`` (TIFF, DNG,
    CR2, NEF, ARW and the inside of a JPEG/HEIC EXIF block)."""
    f.seek(base)
    head = f.read(8)
    if len(head) < 8 or head[:2] not in (b"II", b"MM"):
        return {}
    endian = "<" if head[:2] == b"II" else ">"
    ifd0 = struct.unpack(endian + "I", head[4:8])[0]
    tags = _read_ifd(f, base, ifd0, endian, _WANTED_IFD0)
    sub = tags.pop(TAG_EXIF_IFD, None)
    if isinstance(sub, int) and sub:
        tags.update(_read_ifd(f, base, sub, endian, _WANTED_EXIF))
    return tags


def _jpeg_exif(f) -> dict[int, object]:
    f.seek(2)
    while f.tell() < JPEG_SCAN_LIMIT:
        marker = f.read(4)
        if len(marker) < 4 or marker[0] != 0xFF:
            return {}
        kind = marker[1]
        length = struct.unpack(">H", marker[2:4])[0]
        if kind in (0xDA, 0xD9):  # image data starts: no EXIF before it
            return {}
        if kind == 0xE1:
            body = f.read(length - 2)
            if body[:6] == b"Exif\x00\x00":
                return read_tiff(io.BytesIO(body), 6)
            continue
        f.seek(length - 2, os.SEEK_CUR)
    return {}


def _exif_block(data: bytes | None) -> dict[int, object]:
    if not data:
        return {}
    start = 6 if data[:6] == b"Exif\x00\x00" else 0
    return read_tiff(io.BytesIO(data), start)


def _heif_meta(path: str, meta: Meta) -> dict[int, object]:
    import pillow_heif

    heif = pillow_heif.open_heif(fs(path), convert_hdr_to_8bit=True)
    meta.frames = len(heif)
    return _exif_block(heif.info.get("exif"))


def _pillow_exif(path: str) -> dict[int, object]:
    from PIL import Image

    with Image.open(fs(path)) as im:
        ex = im.getexif()
        tags: dict[int, object] = {k: ex.get(k) for k in _WANTED_IFD0 if k in ex}
        tags.update({k: v for k, v in ex.get_ifd(TAG_EXIF_IFD).items() if k in _WANTED_EXIF})
    return tags


def _fill_exif(meta: Meta, tags: dict[int, object]) -> None:
    meta.exif_original = str(tags.get(TAG_DT_ORIGINAL) or "")
    meta.exif_digitized = str(tags.get(TAG_DT_DIGITIZED) or "")
    meta.exif_datetime = str(tags.get(TAG_DATETIME) or "")
    make, model = str(tags.get(TAG_MAKE) or "").strip(), str(tags.get(TAG_MODEL) or "").strip()
    meta.camera = model if make and model.lower().startswith(make.lower()) else " ".join(x for x in (make, model) if x)
    orient = tags.get(TAG_ORIENTATION)
    meta.orientation = orient if isinstance(orient, int) else 0


# ------------------------------------------------------------------ video atoms
def _atoms(f, start: int, end: int):
    """(type, payload start, payload end) of the boxes between start and end."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        head = f.read(8)
        if len(head) < 8:
            return
        size, kind = struct.unpack(">I4s", head)
        hdr = 8
        if size == 1:
            big = f.read(8)
            if len(big) < 8:
                return
            size = struct.unpack(">Q", big)[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            return
        yield kind, pos + hdr, pos + size
        pos += size


def _child(f, start: int, end: int, kind: bytes) -> tuple[int, int] | None:
    for k, s, e in _atoms(f, start, end):
        if k == kind:
            return s, e
    return None


def _read(f, start: int, end: int) -> bytes:
    f.seek(start)
    return f.read(min(end - start, ATOM_READ_LIMIT))


def _mvhd_time(f, s: int, e: int) -> datetime.datetime | None:
    data = _read(f, s, e)
    if len(data) < 12:
        return None
    if data[0] == 1:
        secs = struct.unpack(">Q", data[4:12])[0]
    else:
        secs = struct.unpack(">I", data[4:8])[0]
    if secs <= 0:
        return None  # 0 = 1904-01-01: not set
    unix = secs - MAC_EPOCH_OFFSET
    if unix <= 0:
        return None
    try:
        return datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=unix)
    except OverflowError:
        return None


def _parse_video_text(text: str) -> tuple[datetime.datetime | None, bool]:
    """(time, is_utc) from "2024-03-15T12:34:56+0900" (local wall clock: offset ignored)
    or "2024-03-15T03:34:56Z" (UTC)."""
    s = text.strip().strip("\x00")
    utc = s.endswith("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(s[:19], fmt), utc
        except ValueError:
            continue
    return None, False


def _meta_box_start(f, s: int, e: int) -> int:
    """QuickTime 'meta' holds boxes directly; the MP4 one is a full box (4 bytes of version/flags first)."""
    f.seek(s)
    head = f.read(8)
    return s if head[4:8] == b"hdlr" else s + 4


def _quicktime_creationdate(f, s: int, e: int) -> str:
    """moov/meta: 'keys' lists names, 'ilst' holds values by 1-based key index."""
    s = _meta_box_start(f, s, e)
    keys = _child(f, s, e, b"keys")
    ilst = _child(f, s, e, b"ilst")
    if not keys or not ilst:
        return ""
    data = _read(f, *keys)
    if len(data) < 8:
        return ""
    count = struct.unpack(">I", data[4:8])[0]
    pos, wanted = 8, 0
    for i in range(1, min(count, 512) + 1):
        if pos + 8 > len(data):
            break
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        if size < 8:
            break
        if data[pos + 8:pos + size] == b"com.apple.quicktime.creationdate":
            wanted = i
            break
        pos += size
    if not wanted:
        return ""
    for k, a, b in _atoms(f, *ilst):
        if struct.unpack(">I", k)[0] == wanted:
            d = _child(f, a, b, b"data")
            if d:
                return _read(f, *d)[8:].decode("utf-8", "replace")
    return ""


def _udta_day(f, s: int, e: int) -> str:
    """(c)day: QuickTime user data (2-byte length, 2-byte language, text) or an iTunes-style meta/ilst item."""
    day = _child(f, s, e, b"\xa9day")
    if day:
        raw = _read(f, *day)
        if raw[4:8] != b"data" and len(raw) >= 4:
            n = struct.unpack(">H", raw[:2])[0]
            return raw[4:4 + n].decode("utf-8", "replace")
    meta = _child(f, s, e, b"meta")
    if meta:
        start = _meta_box_start(f, *meta)
        ilst = _child(f, start, meta[1], b"ilst")
        if ilst:
            item = _child(f, *ilst, b"\xa9day")
            if item:
                d = _child(f, *item, b"data")
                if d:
                    return _read(f, *d)[8:].decode("utf-8", "replace")
    return ""


def _video_meta(f, size: int, meta: Meta) -> None:
    moov = _child(f, 0, size, b"moov")
    if not moov:
        return
    mvhd = _child(f, *moov, b"mvhd")
    if mvhd:
        meta.video_utc = _mvhd_time(f, *mvhd)
    texts = []
    qt_meta = _child(f, *moov, b"meta")
    if qt_meta:
        texts.append(_quicktime_creationdate(f, *qt_meta))
    udta = _child(f, *moov, b"udta")
    if udta:
        texts.append(_udta_day(f, *udta))
    for text in texts:
        when, utc = _parse_video_text(text) if text else (None, False)
        if when is None:
            continue
        if utc:
            meta.video_utc = meta.video_utc or when
        else:
            meta.video_local = when
            return


# ------------------------------------------------------------------ entry points
OWN_READERS = ("MP4", "MOV", "3GP", "JPEG", "TIFF")  # read through a file object the caller may already have open


def read_meta(path: str, fmt: str, size: int, f=None) -> Meta:
    """Read the date candidates of one file. Never raises: a broken file gives an
    empty Meta with ``error`` set, and the file is still organized by name or pair.
    ``f``: the file, already open (scan opens each file once); used for OWN_READERS."""
    meta = Meta()
    if size <= 0:
        return meta
    try:
        if fmt in ("HEIC", "AVIF"):
            _fill_exif(meta, _heif_meta(path, meta))
        elif fmt in OWN_READERS:
            if f is None:
                with open(fs(path), "rb") as own:
                    return read_meta(path, fmt, size, own)
            if fmt in ("MP4", "MOV", "3GP"):
                _video_meta(f, size, meta)
            else:
                _fill_exif(meta, _jpeg_exif(f) if fmt == "JPEG" else read_tiff(f))
        elif fmt in ("PNG", "WEBP"):
            _fill_exif(meta, _pillow_exif(path))
    except Exception as exc:  # broken EXIF, truncated file, decoder refusal: treat as "no metadata"
        meta = Meta(error=f"{exc.__class__.__name__}: {exc}"[:200])
    return meta


def local_from_utc(when: datetime.datetime) -> datetime.datetime:
    """A naive UTC time shown in this computer's time zone (videos only)."""
    stamp = calendar.timegm(when.timetuple())
    try:
        return datetime.datetime.fromtimestamp(stamp)
    except (OverflowError, OSError, ValueError):
        return when


def resolve(meta: Meta, stem: str, mtime: float, opts: DateOptions) -> Resolved:
    """Pick the date by the priority table (pairs are handled in plan.py)."""
    years = opts.years()
    named = patterns.from_name(stem, years, opts.user_patterns)
    source = named.source if named and named.source else ""
    if not source:
        source = patterns.CAMERA if meta.camera else patterns.OTHER
    name_basis = B_NAME_MULTI if named and named.multiple else B_NAME
    exif_candidates = [parse_exif_datetime(meta.exif_original), parse_exif_datetime(meta.exif_digitized),
                       parse_exif_datetime(meta.exif_datetime)]
    for when in exif_candidates:
        if when is None:
            continue
        if when in RESET_DATES:
            # a reset clock: a date in the name is more likely right
            if named:
                return Resolved(named.when, name_basis, source)
            if _in_range(when, years):
                return Resolved(when, B_EXIF_SUSPECT, source)
            continue
        if _in_range(when, years):
            return Resolved(when, B_EXIF, source)
    if meta.video_local and _in_range(meta.video_local, years):
        return Resolved(meta.video_local, B_VIDEO, source)
    if meta.video_utc:
        local = local_from_utc(meta.video_utc)
        if _in_range(local, years):
            return Resolved(local, B_VIDEO, source)
    if named:
        return Resolved(named.when, name_basis, source)
    if opts.use_mtime and mtime > 0:
        when = datetime.datetime.fromtimestamp(mtime)
        if _in_range(when, years):
            return Resolved(when, B_MTIME, source)
    return Resolved(None, "", source)
