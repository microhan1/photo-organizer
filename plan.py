"""Turn a scan into a plan: one Item per photo (with its paired files), the
date it was taken, where it goes, and what happens on the way (move, convert,
duplicate). Pure computation apart from listing target folders and hashing
files whose names collide; nothing is changed on the disk.

Rules that keep a second run quiet (idempotent):
  * the target name comes from the file's own name, and a file never collides with itself;
  * names already on the disk are never taken, so "IMG (2).jpg" stays "IMG (2).jpg";
  * the converted JPG and the HEIC share one name, so the pair is seen next time.
"""
from __future__ import annotations

import dataclasses
import functools
import datetime
import hashlib
import os
import re
import unicodedata

import dates
import i18n
import patterns
from longpath import fs
from scan import Media, ScanResult, is_junk, key_of, stem_key

MOVE, COPY = "move", "copy"
OK, SAME, NODATE, DUP, CONFLICT, CONVERT = "ok", "same", "nodate", "dup", "conflict", "convert"
STATUSES = (OK, CONVERT, NODATE, DUP, CONFLICT, SAME)

DEFAULT_PATTERN = "{yyyy}/{yyyy-mm-dd}"
PRESETS = (DEFAULT_PATTERN, "{yyyy}/{yyyy-mm}", "{yyyy-mm-dd}", "{yyyy}/{source}/{yyyy-mm-dd}")
PLACEHOLDERS = ("yyyy", "mm", "dd", "yyyy-mm", "yyyy-mm-dd", "source", "ext")
MAX_PATH_LEN = 240  # longer targets get a shorter file name (Windows MAX_PATH is 260)
MIN_STEM = 8
BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# the member that gives a pair its date and name: HEIC/AVIF, then RAW, then JPEG, other images, video
PRIMARY_ORDER = {"heic": 0, "avif": 1, "raw": 2, "jpeg": 3, "image": 4, "video": 5, "sidecar": 6}
PAIR_KINDS = {"heic", "avif", "raw", "jpeg"}  # a pair needs one of these
HASH_CHUNK = 1024 * 1024


@dataclasses.dataclass
class Options:
    root: str
    dest: str
    mode: str = MOVE
    pattern: str = DEFAULT_PATTERN
    rename: bool = False  # {yyyy-mm-dd}_{hhmmss}_{name}
    include_nodate: bool = False  # files without a date go to the "No Date" folder
    convert: bool = False
    convert_avif: bool = False
    heic_only: bool = False  # convert in place, organize nothing
    heic_original: str = "keep"  # or "move" to the _Original_HEIC folder
    remove_empty: bool = True
    dupes_action: str = "move"  # or "trash"
    jpg_pair_as_dupe: bool = False
    dates: dates.DateOptions = dataclasses.field(default_factory=dates.DateOptions)
    nodate_name: str = ""
    dupes_name: str = ""
    heic_name: str = ""

    def __post_init__(self) -> None:
        self.root = os.path.abspath(self.root)
        self.dest = os.path.abspath(self.dest or self.root)
        self.nodate_name = self.nodate_name or i18n.t("folder_nodate")
        self.dupes_name = self.dupes_name or i18n.t("folder_dupes")
        self.heic_name = self.heic_name or i18n.t("folder_heic")


@dataclasses.dataclass
class Overrides:
    """What the user changed in the table, by key_of(primary path)."""
    dates: dict[str, datetime.datetime] = dataclasses.field(default_factory=dict)
    dup: dict[str, bool] = dataclasses.field(default_factory=dict)  # True: send to duplicates, False: keep
    unchecked: set[str] = dataclasses.field(default_factory=set)


@dataclasses.dataclass
class Item:
    members: list[Media]  # primary first
    when: datetime.datetime | None = None
    basis: str = ""
    source: str = patterns.OTHER
    status: str = SAME
    notes: list[str] = dataclasses.field(default_factory=list)  # lang keys: note_<x>
    checked: bool = True
    dsts: list[str] = dataclasses.field(default_factory=list)  # per member; equal to the source when it stays
    convert_dst: str = ""  # the JPG made from the HEIC
    heic_dst: str = ""  # where the HEIC original goes after conversion
    dup_of: str = ""
    dup_reason: str = ""  # "exact", "pair", "similar", "name"
    similar: int = 0  # similar-photo group number (0: none)
    suggest_dup: bool = False
    keep: bool = False  # the user said "keep this one": never made a duplicate again
    present: bool = False  # copy mode: an earlier run already copied this photo to its place
    truncated: bool = False

    @property
    def primary(self) -> Media:
        return self.members[0]

    @property
    def key(self) -> str:
        return self.primary.key

    @functools.cached_property
    def moves(self) -> bool:
        """A folder that differs only in letter case is the same folder on Windows: not a move.
        A file name that differs in case is (the user sees the new spelling).
        Computed once, after the targets are final (_finish is the first to ask)."""
        return any(key_of(d) != m.key or os.path.basename(d) != m.name
                   for m, d in zip(self.members, self.dsts) if d)

    @property
    def acts(self) -> bool:
        return self.checked and not self.present and (self.moves or bool(self.convert_dst) or bool(self.heic_dst) or self.trashes)

    @property
    def trashes(self) -> bool:
        return self.status == DUP and not any(self.dsts)

    @property
    def dst(self) -> str:
        return self.dsts[0] if self.dsts else ""


@dataclasses.dataclass
class Plan:
    options: Options
    items: list[Item]
    empty_dirs: list[str] = dataclasses.field(default_factory=list)
    cloud: list[str] = dataclasses.field(default_factory=list)
    denied: list[str] = dataclasses.field(default_factory=list)

    def summary(self) -> dict[str, int]:
        s = {"move": 0, "convert": 0, "nodate": 0, "dupes": 0, "same": 0, "folders": len(self.empty_dirs)}
        for i in self.items:
            if i.status == DUP:
                s["dupes"] += 1
            elif i.status == NODATE:
                s["nodate"] += 1
            elif not i.acts:
                s["same"] += 1
            if i.status != DUP and i.checked and i.moves:
                s["move"] += 1
            if i.checked and i.convert_dst:
                s["convert"] += 1
        return s

    def actionable(self) -> int:
        return sum(1 for i in self.items if i.acts) + (len(self.empty_dirs) if self.options.remove_empty else 0)

    def blocked_groups(self) -> list[list[Item]]:
        """Duplicate / similar groups where every member would leave: at least one has to stay."""
        groups: dict[str, list[Item]] = {}
        for i in self.items:
            if i.similar:
                groups.setdefault(f"s{i.similar}", []).append(i)
        by_key = {i.key: i for i in self.items}
        for i in self.items:
            if i.status == DUP and i.dup_of and key_of(i.dup_of) in by_key:
                keeper = by_key[key_of(i.dup_of)]
                groups.setdefault(f"d{keeper.key}", [keeper]).append(i)
        return [g for g in groups.values() if len(g) > 1 and all(x.status == DUP and x.checked for x in g)]


# ------------------------------------------------------------------ pattern
def validate_pattern(text: str) -> str:
    """"" when fine, else a lang key."""
    if not text.strip():
        return "err_pattern_empty"
    for name in re.findall(r"\{([^}]*)\}", text):
        if name not in PLACEHOLDERS:
            return "err_pattern_unknown"
    if text.count("{") != text.count("}"):
        return "err_pattern_braces"
    if re.match(r"^([A-Za-z]:|[\\/])", text.strip()) or ".." in re.split(r"[\\/]", text):
        return "err_pattern_absolute"
    return ""


def safe_name(text: str) -> str:
    text = BAD_CHARS.sub("_", text).strip().rstrip(".")
    return text or "_"


def render_pattern(pattern: str, when: datetime.datetime, source: str, ext: str) -> list[str]:
    """Folder names for one date: "{yyyy}/{yyyy-mm-dd}" -> ["2024", "2024-03-15"]. ISO dates in every language."""
    values = {"yyyy": f"{when.year:04d}", "mm": f"{when.month:02d}", "dd": f"{when.day:02d}",
              "yyyy-mm": f"{when.year:04d}-{when.month:02d}", "yyyy-mm-dd": when.strftime("%Y-%m-%d"),
              "source": i18n.t(f"source_{source or patterns.OTHER}"), "ext": ext.lstrip(".").lower() or "_"}
    out = []
    for seg in re.split(r"[\\/]+", pattern.strip()):
        text = re.sub(r"\{([^}]*)\}", lambda m: values.get(m.group(1), m.group(0)), seg)
        if text.strip():
            out.append(safe_name(text))
    return out


def renamed_stem(when: datetime.datetime, stem: str) -> str:
    """Sortable name: 2024-03-15_123456_IMG_0001. A prefix this option wrote earlier is replaced, never
    stacked (a date changed by hand would otherwise give "2020-…_2019-…_name")."""
    return when.strftime("%Y-%m-%d_%H%M%S_") + patterns.strip_renamed(stem)


def fit_stem(folder: str, stem: str, exts: list[str]) -> tuple[str, bool]:
    """Shorten the name so the longest member path stays within MAX_PATH_LEN."""
    longest = max((len(e) for e in exts), default=0)
    room = MAX_PATH_LEN - len(os.path.join(folder, "")) - longest
    if len(stem) <= room:
        return stem, False
    return stem[: max(room, MIN_STEM)].rstrip(" ."), True


# ------------------------------------------------------------------ grouping
def group_pairs(media: list[Media]) -> list[list[Media]]:
    """Files in one folder with one name and different extensions travel together
    (HEIC+JPG, RAW+JPG, photo+MOV live photo, .aae/.xmp). A folder holding two files
    of one kind under one name (a.jpg and a.jpeg) does not form a pair."""
    buckets: dict[tuple[str, str], list[Media]] = {}
    for m in media:
        buckets.setdefault((key_of(m.folder), stem_key(m.name)), []).append(m)
    out: list[list[Media]] = []
    for files in buckets.values():
        files.sort(key=lambda m: (PRIMARY_ORDER.get(m.kind, 9), m.name))
        kinds = [m.kind if m.kind != "sidecar" else m.ext.lower() for m in files]
        pairable = len(files) > 1 and len(set(kinds)) == len(kinds) and files[0].kind in PAIR_KINDS
        if pairable and sum(1 for m in files if m.kind == "image") == 0:
            out.append(files)
        else:
            out.extend([m] for m in files)
    out.sort(key=lambda g: key_of(g[0].path))
    return out


def _date_of_group(item: Item, opts: Options, manual: datetime.datetime | None) -> None:
    if manual is not None:
        item.when, item.basis = manual, dates.B_MANUAL
        item.source = dates.resolve(item.primary.meta, item.primary.stem, item.primary.mtime, opts.dates).source
        return
    members = item.members
    if opts.jpg_pair_as_dupe and item.primary.kind in ("heic", "avif", "raw"):
        # the JPG of this pair is about to leave as a duplicate: its date must not decide where the photo
        # goes, or the next run (JPG gone) would read another date and move the photo again (LESSONS A15)
        members = [m for m in members if m.kind != "jpeg"]
    resolved = [dates.resolve(m.meta, m.stem, m.mtime, opts.dates) for m in members]
    first = resolved[0]
    item.source = first.source
    if first.when is not None:
        item.when, item.basis = first.when, first.basis
        return
    for r in resolved[1:]:  # borrow from the pair
        if r.when is not None and r.basis != dates.B_MTIME:
            item.when, item.basis = r.when, dates.B_PAIR
            return
    for r in resolved[1:]:
        if r.when is not None:
            item.when, item.basis = r.when, r.basis
            return


# ------------------------------------------------------------------ hashing
def sha1_of(path: str) -> str:
    h = hashlib.sha1()
    with open(fs(path), "rb") as f:
        while chunk := f.read(HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def media_sha1(m: Media) -> str:
    if not m.sha1:
        m.sha1 = sha1_of(m.path)
    return m.sha1


class _Listing:
    """lexists for many targets in a few folders: one listing per folder. Names compare
    with normcase as Windows does, and also NFC, so the tool never makes a second name
    that only differs in Unicode normalization."""

    def __init__(self) -> None:
        self._dirs: dict[str, dict[str, str]] = {}

    def _names(self, folder: str) -> dict[str, str]:
        key = os.path.normcase(folder)
        names = self._dirs.get(key)
        if names is None:
            try:
                listing = os.listdir(fs(folder))
            except OSError:
                listing = []
            names = {os.path.normcase(unicodedata.normalize("NFC", n)): n for n in listing}
            self._dirs[key] = names
        return names

    def existing(self, path: str) -> str:
        """The path of what already has this name ("" when free)."""
        folder, name = os.path.split(path)
        real = self._names(folder).get(os.path.normcase(unicodedata.normalize("NFC", name)))
        return os.path.join(folder, real) if real else ""


# ------------------------------------------------------------------ placing
@dataclasses.dataclass
class _Ctx:
    opts: Options
    claimed: dict[str, str]  # target_key -> key of the item that claimed it
    counters: dict[str, int]
    disk: _Listing
    scanned: dict[str, Media]  # key_of(path) -> media (files this plan knows)


def _member_names(item: Item, stem: str) -> list[str]:
    names = [stem + m.ext for m in item.members]
    if item.convert_dst:
        names.append(stem + ".jpg")
    return names


def _tkey(path: str) -> str:
    """scan.target_key without abspath: every path the plan builds is already absolute and
    normalized (scan paths, Options.dest), and abspath was 1.9 million calls on 100,000 files."""
    return os.path.normcase(unicodedata.normalize("NFC", path))


@functools.lru_cache(maxsize=4096)
def _folders(pattern: str, day: datetime.date, source: str, ext: str, lang: str) -> tuple[str, ...]:
    """Folder names for one day (cached: most photos share a day with others). ``lang`` is in
    the key because {source} names come from the language file."""
    return tuple(render_pattern(pattern, datetime.datetime(day.year, day.month, day.day), source, ext))


@functools.lru_cache(maxsize=1)
def _nodate_names() -> frozenset[str]:
    """The "No Date" folder's name in every language (normalized for comparing)."""
    return frozenset(unicodedata.normalize("NFC", n).casefold() for n in i18n.all_values("folder_nodate"))


def _nodate_folder_in_place(item: Item, opts: Options) -> str:
    """When the photo already sits in the destination's "No Date" folder, whatever language named it,
    that folder is its place: switching the language must not move everything into a renamed folder."""
    folder = item.primary.folder
    if key_of(os.path.dirname(folder)) != key_of(opts.dest):
        return ""
    name = os.path.basename(folder)
    return name if unicodedata.normalize("NFC", name).casefold() in _nodate_names() else ""


def _copy_present(item: Item, folder: str, stem: str, ctx: "_Ctx") -> bool:
    """Copy mode: every file of this photo is already at folder/stem with the very same bytes, and the
    file is not part of this scan (an earlier run put it there). Then there is nothing to copy: running
    the same copy again must not fill the duplicates folder with copies it already made."""
    for m in item.members:
        other = ctx.disk.existing(os.path.join(folder, stem + m.ext))
        if not other or key_of(other) in ctx.scanned:
            return False
        try:
            if os.path.getsize(fs(other)) != m.size or sha1_of(other) != media_sha1(m):
                return False
        except OSError:
            return False
    return not item.convert_dst or bool(ctx.disk.existing(os.path.join(folder, stem + ".jpg")))


def _already_copied(item: Item, folder: str, stem: str, ctx: "_Ctx") -> bool:
    """Walk the names this photo would get ("x", "x (2)", ...) while one exists on the disk; when one
    of them is this photo's own earlier copy, the item is done: nothing to do, nothing numbered."""
    candidate, n = stem, 1
    while ctx.disk.existing(os.path.join(folder, candidate + item.primary.ext)):
        if _copy_present(item, folder, candidate, ctx):
            item.present = True
            item.status = SAME
            item.dsts = [m.path for m in item.members]
            item.convert_dst = ""
            return True
        n += 1
        candidate = f"{stem} ({n})"
    return False


def _target_folder(item: Item, opts: Options) -> list[str]:
    if opts.heic_only:
        return []
    if item.status == DUP:
        return [opts.dupes_name]
    if item.when is None:
        if not opts.include_nodate:
            return []
        return [_nodate_folder_in_place(item, opts) or opts.nodate_name]
    ext = item.primary.ext.lower()
    if item.convert_dst and opts.heic_original == "move":
        ext = ".jpg"  # the HEIC leaves for the originals folder: the JPG stays as the photo, so {ext} is "jpg" every run
    return list(_folders(opts.pattern, item.when.date(), item.source, ext, i18n.current_lang()))


def _base_stem(item: Item, opts: Options) -> str:
    stem = item.primary.stem
    if opts.rename and item.when is not None and item.status != DUP and not opts.heic_only:
        stem = renamed_stem(item.when, stem)
    return stem


def _stays(item: Item, opts: Options) -> bool:
    if opts.heic_only:
        return True
    return item.when is None and not opts.include_nodate and item.status != DUP


def _own(item: Item) -> set[str]:
    return {_tkey(m.path) for m in item.members}


def _free(item: Item, folder: str, stem: str, ctx: _Ctx) -> bool:
    own = _own(item)
    for name in _member_names(item, stem):
        path = os.path.join(folder, name)
        tk = _tkey(path)
        if tk in own:
            continue
        if tk in ctx.claimed and ctx.claimed[tk] != item.key:
            return False
        if ctx.disk.existing(path):
            return False
    return True


def _same_content(item: Item, folder: str, stem: str, ctx: _Ctx) -> str:
    """When the plain name is taken by a file with the very same bytes as the primary,
    that file's path (the item is then a duplicate, not a conflict)."""
    path = os.path.join(folder, stem + item.primary.ext)
    other = ctx.disk.existing(path)
    tk = _tkey(path)
    if not other and tk in ctx.claimed:
        other_media = ctx.scanned.get(ctx.claimed[tk])
        other = other_media.path if other_media else ""
    if not other or key_of(other) == item.key:
        return ""
    known = ctx.scanned.get(key_of(other))
    try:
        size = known.size if known else os.path.getsize(other)
        if size != item.primary.size or size == 0:
            return ""
        theirs = media_sha1(known) if known else sha1_of(other)
        return other if theirs == media_sha1(item.primary) else ""
    except OSError:
        return ""


def _claim(item: Item, folder: str, stem: str, ctx: _Ctx) -> None:
    for name in _member_names(item, stem):
        ctx.claimed[_tkey(os.path.join(folder, name))] = item.key
    item.dsts = [os.path.join(folder, stem + m.ext) for m in item.members]
    if item.convert_dst:
        item.convert_dst = os.path.join(folder, stem + ".jpg")


def _place(item: Item, ctx: _Ctx) -> None:
    """Set the item's targets. A second run stays quiet without any special pass: the
    target name comes from the file's own name ("x (2).jpg" aims at "x (2).jpg"), a file
    never collides with itself, and names already on the disk are never taken."""
    opts = ctx.opts
    if _stays(item, opts):
        item.dsts = [m.path for m in item.members]
        if item.convert_dst:
            stem = item.primary.stem
            if not _free(item, item.primary.folder, stem, ctx):
                n = 2
                while not _free(item, item.primary.folder, f"{stem} ({n})", ctx):
                    n += 1
                stem = f"{stem} ({n})"
            item.convert_dst = os.path.join(item.primary.folder, stem + ".jpg")
            ctx.claimed[_tkey(item.convert_dst)] = item.key
        return
    if item.status == DUP and opts.dupes_action == "trash":
        item.dsts = ["" for _ in item.members]
        return
    base = opts.dest
    folder = os.path.join(base, *_target_folder(item, opts))
    stem, item.truncated = fit_stem(folder, _base_stem(item, opts), _member_names(item, "") or [""])
    if opts.mode == COPY and _already_copied(item, folder, stem, ctx):
        return
    if item.status != DUP and not item.keep:
        twin = _same_content(item, folder, stem, ctx)
        if twin:
            item.status, item.dup_of, item.dup_reason = DUP, twin, "name"
            if opts.dupes_action == "trash":
                item.dsts = ["" for _ in item.members]
                return
            folder = os.path.join(base, opts.dupes_name)
            stem, item.truncated = fit_stem(folder, item.primary.stem, _member_names(item, ""))
    name_key = _tkey(os.path.join(folder, stem))
    n = ctx.counters.get(name_key, 1)
    candidate = stem if n == 1 else f"{stem} ({n})"
    while not _free(item, folder, candidate, ctx):
        n += 1
        candidate = f"{stem} ({n})"
    ctx.counters[name_key] = n
    if n > 1 and item.status not in (DUP,):
        item.status = CONFLICT
    _claim(item, folder, candidate, ctx)
    if all(key_of(d) == m.key and os.path.basename(d) == m.name for m, d in zip(item.members, item.dsts)):
        item.dsts = [m.path for m in item.members]  # where it already is (the folder's spelling included)
        if item.status == CONFLICT:
            item.status = SAME  # "x (2).jpg" from an earlier run, already in place


# ------------------------------------------------------------------ build
def group_items(scan: ScanResult, opts: Options, overrides: Overrides | None = None) -> list[Item]:
    """Items with their pair files and dates only (no targets): what duplicate finding needs."""
    ov = overrides or Overrides()
    items = []
    for members in group_pairs(scan.media):
        item = Item(members)
        if item.primary.kind != "sidecar":
            _date_of_group(item, opts, ov.dates.get(item.key))
        items.append(item)
    return items


def build(scan: ScanResult, opts: Options, dupes: dict[str, tuple[str, str]] | None = None,
          similar: dict[str, tuple[int, bool]] | None = None, overrides: Overrides | None = None,
          grouped: list[Item] | None = None) -> Plan:
    """``dupes``: key_of(primary) -> (path kept, reason) from dedupe.find_exact.
    ``similar``: key_of(primary) -> (group number, suggested as duplicate).
    ``grouped``: group_items() already made for duplicate finding with the same options
    (reused instead of grouping and dating 100,000 files twice; the list is consumed)."""
    ov = overrides or Overrides()
    dupes = dupes or {}
    similar = similar or {}
    items: list[Item] = []
    for item in grouped if grouped is not None else group_items(scan, opts, ov):
        members = item.members
        primary = item.primary
        if primary.kind == "sidecar":  # .aae / .xmp left without its photo
            item.dsts = [primary.path]
            items.append(item)
            continue
        if primary.size == 0:
            item.notes.append("empty")
            item.when, item.basis = (item.when, item.basis) if item.basis == dates.B_MANUAL else (None, "")
        if primary.meta.frames > 1:
            item.notes.append("burst")
        if primary.meta.error:
            item.notes.append("meta_error")
        if item.when is None:
            item.status = NODATE
        kind_ok = primary.is_heif or (opts.convert_avif and primary.is_avif)
        has_jpeg = any(m.kind == "jpeg" for m in members[1:])
        if (opts.convert or opts.heic_only) and kind_ok and not has_jpeg and primary.size > 0:
            item.convert_dst = "pending"
        if opts.jpg_pair_as_dupe and has_jpeg and primary.kind in ("heic", "avif", "raw"):
            item.notes.append("pair_jpg")
        sim = similar.get(item.key)
        if sim:
            item.similar, item.suggest_dup = sim
        dup = dupes.get(item.key)
        user = ov.dup.get(item.key)
        item.keep = user is False
        if (dup and user is not False) or user:
            item.status = DUP
            item.dup_of, item.dup_reason = (dup if dup else ("", "similar"))
            item.convert_dst = ""
        if item.key in ov.unchecked:
            item.checked = False
        items.append(item)
    if opts.jpg_pair_as_dupe:
        items = _split_jpg_pairs(items, opts)
    ctx = _Ctx(opts, {}, {}, _Listing(), {m.key: m for m in scan.media})
    for item in items:
        if not item.checked:
            item.dsts = [m.path for m in item.members]  # shown greyed, claims nothing
            if item.convert_dst == "pending":
                item.convert_dst = ""
            continue
        if item.primary.kind == "sidecar":  # an .aae / .xmp without its photo stays (dsts set above)
            continue
        _place(item, ctx)
    for item in items:
        _finish(item, opts, ctx)
    plan = Plan(opts, items, cloud=list(scan.cloud), denied=list(scan.denied))
    if opts.remove_empty and not opts.heic_only:
        plan.empty_dirs = _empty_dirs(items, scan, opts)
    return plan


def _split_jpg_pairs(items: list[Item], opts: Options) -> list[Item]:
    """Setting "treat the JPG of a HEIC/RAW pair as a duplicate": the JPG becomes its own
    item and goes to the duplicates folder."""
    out = []
    for item in items:
        out.append(item)
        if "pair_jpg" not in item.notes or item.status == DUP:
            continue
        jpg = next(m for m in item.members[1:] if m.kind == "jpeg")
        item.members.remove(jpg)
        twin = Item([jpg], item.when, item.basis, item.source, DUP, dup_of=item.primary.path, dup_reason="pair",
                    checked=item.checked)
        out.append(twin)
    return out


def _finish(item: Item, opts: Options, ctx: _Ctx) -> None:
    if not item.dsts:
        item.dsts = [m.path for m in item.members]
    if item.convert_dst == "pending":
        item.convert_dst = ""
    if item.convert_dst and opts.heic_original == "move" and item.checked:
        primary_dst = item.dsts[0] or item.primary.path
        rel = os.path.relpath(os.path.dirname(primary_dst), opts.dest)
        rel_parts = [] if rel == "." or rel.startswith("..") else rel.split(os.sep)
        folder = os.path.join(opts.dest, opts.heic_name, *rel_parts)
        name = os.path.basename(primary_dst)
        stem, ext = os.path.splitext(name)
        path, n = os.path.join(folder, name), 1
        while _tkey(path) in ctx.claimed or ctx.disk.existing(path):
            n += 1
            path = os.path.join(folder, f"{stem} ({n}){ext}")
        ctx.claimed[_tkey(path)] = item.key
        item.heic_dst = path
    if item.status in (DUP, NODATE):
        return
    if item.convert_dst and item.checked:
        item.status = CONVERT if item.status != CONFLICT else CONFLICT
    elif item.status != CONFLICT:
        item.status = OK if item.moves else SAME


# ------------------------------------------------------------------ empty folders
def _empty_dirs(items: list[Item], scan: ScanResult, opts: Options) -> list[str]:
    leaving = {m.key for i in items if i.acts for m, d in zip(i.members, i.dsts)
               if opts.mode == MOVE and (not d or key_of(d) != m.key)}
    keep: set[str] = {key_of(scan.root)}
    targets = [opts.dest] + [os.path.dirname(d) for i in items if i.acts for d in i.dsts if d]
    targets += [os.path.dirname(i.heic_dst) for i in items if i.heic_dst]
    for d in targets:
        while True:
            k = key_of(d)
            if k in keep:
                break
            keep.add(k)
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    if opts.mode != MOVE:
        return []
    empty: dict[str, bool] = {}
    for dkey in sorted(scan.dirs, key=lambda k: -k.count(os.sep)):
        info = scan.dirs[dkey]
        if dkey in keep:
            empty[dkey] = False
            continue
        files_ok = all(is_junk(n) or key_of(os.path.join(info.path, n)) in leaving for n in info.files)
        subs_ok = all(empty.get(key_of(os.path.join(info.path, s)), False) for s in info.subdirs)
        empty[dkey] = files_ok and subs_ok
    had_files = {k for k in empty if empty[k] and scan.dirs[k].files and
                 any(not is_junk(n) for n in scan.dirs[k].files)}
    # only folders this run empties (or that hold only emptied folders), never ones that were empty already
    def emptied(k: str) -> bool:
        if k in had_files:
            return True
        info = scan.dirs[k]
        return any(emptied(key_of(os.path.join(info.path, s))) for s in info.subdirs)
    return [scan.dirs[k].path for k in sorted(empty, key=lambda k: (-k.count(os.sep), k)) if empty[k] and emptied(k)]
