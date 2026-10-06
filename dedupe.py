"""Duplicates: identical files (SHA-1) and similar photos (perceptual hash).

Stage 1 (exact): files of the same size are hashed; in each group one stays,
the rest are marked for the duplicates folder.
Stage 2 (pairs, HEIC+JPG / RAW+JPG) is handled by plan.group_pairs: a pair is
never a duplicate.
Stage 3 (similar, optional): pHash with Hamming distance <= threshold, the four
rotations compared. Groups are only shown; a suggestion is made when one copy is
clearly the lesser one (a KakaoTalk re-compression, a smaller resolution, a
re-save) and never for bursts.

pHash is the usual one (as in the imagehash library): 32x32 greyscale, 2-D DCT,
the 8x8 lowest frequencies compared to their median. It is computed here in plain
Python so the program does not carry scipy; only the 8 lowest DCT rows and
columns are needed, which is cheap.
"""
from __future__ import annotations

import math
import re
import threading
from typing import Callable

import patterns
import plan as plan_mod
from longpath import fs
from scan import Media, key_of

HASH_SIZE = 8
IMG_SIZE = 32
DEFAULT_THRESHOLD = 6
RESAVE_DISTANCE = 2  # this close, same size, same time: one photo saved twice
BANDS = 7  # distance <= 6 means at least one of 7 bands of the 64 bits is equal
SIMILAR_KINDS = {"heic", "avif", "jpeg", "image", "raw"}
# seconds per photo, measured on a laptop SSD (pHash needs a decode); used for the estimate
SECONDS_PER_PHOTO = {"jpeg": 0.02, "heic": 0.09, "avif": 0.09, "raw": 0.25, "image": 0.04}
ProgressFn = Callable[[int, int], None]
# names people and programs give to copies (Windows "- Copy", "(2)", Korean / Japanese / Chinese "copy")
COPY_NAME = re.compile(r"(\bcopy\b|- copy|\(\d+\)$|복사본|사본|コピー|副本)", re.IGNORECASE)

_COS = [[math.cos(math.pi * (2 * x + 1) * u / (2 * IMG_SIZE)) for x in range(IMG_SIZE)] for u in range(HASH_SIZE)]


# ------------------------------------------------------------------ exact
def find_exact(items: list[plan_mod.Item], progress: ProgressFn | None = None,
               cancel: threading.Event | None = None) -> dict[str, tuple[str, str]]:
    """key_of(duplicate primary) -> (path of the copy that stays, "exact")."""
    by_size: dict[tuple[int, str], list[plan_mod.Item]] = {}
    for item in items:
        m = item.primary
        if m.size > 0 and m.kind != "sidecar":
            # same size and same first 64 KB (hashed during the scan): only these are read in full
            by_size.setdefault((m.size, m.quick), []).append(item)
    candidates = [g for g in by_size.values() if len(g) > 1]
    total = sum(len(g) for g in candidates)
    done = 0
    out: dict[str, tuple[str, str]] = {}
    for group in candidates:
        by_hash: dict[str, list[plan_mod.Item]] = {}
        for item in group:
            if cancel is not None and cancel.is_set():
                return out
            try:
                by_hash.setdefault(plan_mod.media_sha1(item.primary), []).append(item)
            except OSError:
                pass  # unreadable now: not a duplicate of anything
            done += 1
            if progress is not None:
                progress(done, total)
        for same in by_hash.values():
            if len(same) < 2:
                continue
            same.sort(key=_keep_order)
            keeper = same[0]
            for other in same[1:]:
                out[other.key] = (keeper.primary.path, "exact")
    return out


def _is_kakao(m: Media) -> bool:
    return patterns.source_of(m.stem) == patterns.KAKAO


def looks_like_a_copy(stem: str) -> bool:
    return bool(COPY_NAME.search(stem))


def _keep_order(item: plan_mod.Item) -> tuple:
    """Which identical copy stays: one with its pair files, not a KakaoTalk copy, one whose
    date came from EXIF, not named like a copy ("copy", "(2)", "복사본"), the shortest path,
    then the path itself."""
    m = item.primary
    return (-len(item.members), _is_kakao(m), item.basis not in ("exif",), looks_like_a_copy(m.stem),
            len(m.path), m.key)


# ------------------------------------------------------------------ pHash
def _dct_low(pixels: list[float]) -> list[list[float]]:
    """The 8x8 lowest-frequency 2-D DCT-II coefficients of a 32x32 image (row-major list)."""
    n = IMG_SIZE
    rows = [pixels[i * n:(i + 1) * n] for i in range(n)]
    # along x for every row, keep 8 frequencies
    tmp = [[sum(c * v for c, v in zip(_COS[u], row)) for u in range(HASH_SIZE)] for row in rows]
    # along y, keep 8 frequencies
    return [[sum(_COS[v][y] * tmp[y][u] for y in range(n)) for u in range(HASH_SIZE)] for v in range(HASH_SIZE)]


def _bits(block: list[list[float]]) -> int:
    flat = [x for row in block for x in row]
    med = sorted(flat)[len(flat) // 2 - 1: len(flat) // 2 + 1]
    median = (med[0] + med[1]) / 2
    out = 0
    for x in flat:
        out = (out << 1) | (1 if x > median else 0)
    return out


def rotations(block: list[list[float]]) -> tuple[int, int, int, int]:
    """Hashes of the image turned 0/90/180/270 degrees, from one DCT: mirroring an axis
    flips the sign of the odd frequencies on it, and a quarter turn is a transpose plus
    a mirror."""
    n = HASH_SIZE
    r0 = block
    r180 = [[block[v][u] * (-1) ** (u + v) for u in range(n)] for v in range(n)]
    r90 = [[block[u][v] * (-1) ** v for u in range(n)] for v in range(n)]
    r270 = [[block[u][v] * (-1) ** u for u in range(n)] for v in range(n)]
    return _bits(r0), _bits(r90), _bits(r180), _bits(r270)


def phash_image(im) -> tuple[int, int, int, int]:
    from PIL import Image

    small = im.convert("L").resize((IMG_SIZE, IMG_SIZE), Image.Resampling.LANCZOS)
    return rotations(_dct_low([float(p) for p in small.tobytes()]))  # one byte per pixel in mode L


def phash_file(m: Media) -> tuple[tuple[int, int, int, int], tuple[int, int]] | None:
    """(rotation hashes, (width, height)) or None when the picture cannot be decoded."""
    from PIL import Image, ImageOps

    try:
        if m.is_heif or m.is_avif:
            import pillow_heif

            pillow_heif.register_heif_opener()
        with Image.open(fs(m.path)) as im:
            size = im.size
            im.draft("RGB", (IMG_SIZE * 4, IMG_SIZE * 4))  # JPEG: decode at 1/8 scale, much faster
            im = ImageOps.exif_transpose(im)
            if getattr(im, "n_frames", 1) > 1:
                im.seek(0)
            return phash_image(im), size
    except Exception:
        return None


def distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _bands(h: int) -> list[tuple[int, int]]:
    width = math.ceil(64 / BANDS)
    return [(i, (h >> (i * width)) & ((1 << width) - 1)) for i in range(BANDS)]


def estimate_seconds(items: list[plan_mod.Item]) -> float:
    return sum(SECONDS_PER_PHOTO.get(i.primary.kind, 0.05) for i in items if i.primary.kind in SIMILAR_KINDS)


def find_similar(items: list[plan_mod.Item], threshold: int = DEFAULT_THRESHOLD,
                 progress: ProgressFn | None = None,
                 cancel: threading.Event | None = None) -> dict[str, tuple[int, bool]]:
    """key_of(primary) -> (group number, suggested as the duplicate)."""
    photos = [i for i in items if i.primary.kind in SIMILAR_KINDS and i.primary.size > 0]
    for n, item in enumerate(photos, 1):
        if cancel is not None and cancel.is_set():
            return {}
        m = item.primary
        if m.phash is None:
            got = phash_file(m)
            if got:
                m.phash, m.dims = got
        if progress is not None:
            progress(n, len(photos))
    hashed = [i for i in photos if i.primary.phash]
    # candidates through equal bands, then the real distance over the four rotations
    index: dict[tuple[int, int], list[int]] = {}
    for idx, item in enumerate(hashed):
        for h in item.primary.phash:
            for band in _bands(h):
                index.setdefault(band, []).append(idx)
    parent = list(range(len(hashed)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    closest: dict[tuple[int, int], int] = {}
    for idx, item in enumerate(hashed):
        h0 = item.primary.phash[0]
        seen: set[int] = set()
        for band in _bands(h0):
            for other in index.get(band, ()):
                if other <= idx or other in seen:
                    continue
                seen.add(other)
                d = min(distance(h0, h) for h in hashed[other].primary.phash)
                if d <= threshold:
                    closest[(idx, other)] = d
                    parent[find(other)] = find(idx)
    groups: dict[int, list[int]] = {}
    for idx in range(len(hashed)):
        groups.setdefault(find(idx), []).append(idx)
    out: dict[str, tuple[int, bool]] = {}
    number = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        number += 1
        group = [hashed[i] for i in members]
        dists = [d for (a, b), d in closest.items() if a in members and b in members]
        suggested = suggest(group, max(dists) if dists else 0)
        for item in group:
            out[item.key] = (number, item.key in suggested)
    return out


def _pixels(item: plan_mod.Item) -> int:
    d = item.primary.dims
    return d[0] * d[1] if d else 0


def suggest(group: list[plan_mod.Item], spread: int) -> set[str]:
    """Which members look like the lesser copies (keys). Nothing for a burst."""
    kakao = [i for i in group if _is_kakao(i.primary)]
    if kakao and len(kakao) < len(group):
        return {i.key for i in kakao}
    best = max(_pixels(i) for i in group)
    smaller = [i for i in group if _pixels(i) < best]
    if smaller:
        return {i.key for i in smaller}
    times = {i.when for i in group}
    if spread <= RESAVE_DISTANCE and len(times) == 1:  # one photo saved twice: keep the bigger file
        keep = max(group, key=lambda i: (i.primary.size, -len(i.primary.path)))
        return {i.key for i in group if i is not keep}
    return set()
