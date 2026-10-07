"""settings.json values with type checking, and the glue that turns them into
plan.Options and scan exclusions (shared by the GUI and the CLI).

The file sits next to the exe where anyone can edit it, so nothing in it is
trusted: a wrong type or an unknown value falls back to the default.
"""
from __future__ import annotations

import dataclasses
import os

import convert as convert_mod
import dates
import dedupe as dedupe_mod
import i18n
import patterns
import plan as plan_mod
from scan import key_of


@dataclasses.dataclass
class Prefs:
    theme: str = "light"  # light, dark, system
    pattern: str = plan_mod.DEFAULT_PATTERN
    dest: str = ""  # "" = organize inside the source folder
    mode: str = plan_mod.MOVE
    rename: bool = False
    use_mtime: bool = False
    include_nodate: bool = False
    year_min: int = 1990
    year_max: int = 0  # 0 = this year
    name_patterns: list = dataclasses.field(default_factory=list)
    convert: bool = False
    convert_avif: bool = False
    quality: int = convert_mod.DEFAULT_QUALITY
    icc: str = "keep"  # keep, srgb
    heic_original: str = "keep"  # keep, move
    dedupe: bool = True
    similar: bool = False
    similar_threshold: int = dedupe_mod.DEFAULT_THRESHOLD
    jpg_pair_as_dupe: bool = False
    dupes_action: str = "move"  # move, trash
    remove_empty: bool = True
    thumbnails: bool = False
    last_log: str = ""

    def date_options(self) -> dates.DateOptions:
        return dates.DateOptions(self.use_mtime, self.year_min, self.year_max, tuple(self.name_patterns))


CHOICES = {"theme": ("light", "dark", "system"), "mode": (plan_mod.MOVE, plan_mod.COPY), "icc": ("keep", "srgb"),
           "heic_original": ("keep", "move"), "dupes_action": ("move", "trash")}
RANGES = {"quality": (convert_mod.MIN_QUALITY, convert_mod.MAX_QUALITY), "year_min": (1900, 2100), "year_max": (0, 2100),
          "similar_threshold": (0, 16)}


def load() -> Prefs:
    raw = i18n.load_settings()
    default = Prefs()
    values = {}
    for f in dataclasses.fields(Prefs):
        want = type(getattr(default, f.name))
        value = raw.get(f.name, getattr(default, f.name))
        ok = isinstance(value, want) and (want is bool or not isinstance(value, bool))
        values[f.name] = value if ok else getattr(default, f.name)
    p = Prefs(**values)
    for name, allowed in CHOICES.items():
        if getattr(p, name) not in allowed:
            setattr(p, name, getattr(default, name))
    for name, (lo, hi) in RANGES.items():
        if not lo <= getattr(p, name) <= hi:
            setattr(p, name, getattr(default, name))
    p.name_patterns = [s for s in p.name_patterns if isinstance(s, str) and s.strip()]
    if not p.pattern.strip() or plan_mod.validate_pattern(p.pattern):
        p.pattern = default.pattern
    return p


def save(p: Prefs) -> None:
    settings = i18n.load_settings()
    settings.update(dataclasses.asdict(p))
    i18n.save_settings(settings)


def bad_patterns(p: Prefs) -> list[str]:
    return patterns.compile_user(p.name_patterns)[1]


def special_folders(root: str, dest: str) -> list[str]:
    """Folders a scan never enters: the destination when it lies inside the source (no
    endless recursion), and the duplicates / original-HEIC folders in every language
    (switching the language must not turn old duplicates into photos to organize)."""
    root, dest = os.path.abspath(root), os.path.abspath(dest or root)
    out = []
    if key_of(dest) != key_of(root) and key_of(dest).startswith(key_of(root).rstrip(os.sep) + os.sep):
        out.append(dest)
    for base in {root, dest}:
        for key in ("folder_dupes", "folder_heic"):
            out.extend(os.path.join(base, name) for name in i18n.all_values(key))
    return out


def options(p: Prefs, root: str, dest: str | None = None, mode: str | None = None, heic_only: bool = False,
            convert_on: bool | None = None) -> plan_mod.Options:
    wants_convert = p.convert if convert_on is None else convert_on
    # "the JPG of a HEIC pair is a duplicate" and "convert HEIC to JPG" contradict each other: the
    # converted JPG would be sent away as a duplicate and made again on the next run (LESSONS A10).
    # "Convert only" mode has no duplicates, so it keeps working.
    wants_convert = wants_convert and (heic_only or not p.jpg_pair_as_dupe)
    return plan_mod.Options(
        root=root, dest=dest or p.dest or root, mode=mode or p.mode, pattern=p.pattern, rename=p.rename,
        include_nodate=p.include_nodate, convert=wants_convert,
        convert_avif=p.convert_avif and convert_mod.avif_supported(), heic_only=heic_only,
        heic_original=p.heic_original, remove_empty=p.remove_empty, dupes_action=p.dupes_action,
        jpg_pair_as_dupe=p.jpg_pair_as_dupe, dates=p.date_options())
