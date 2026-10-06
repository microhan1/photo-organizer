from __future__ import annotations

import hashlib
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "samples"))

import i18n  # noqa: E402
import make_samples as ms  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path_factory, monkeypatch):
    """Every test gets its own settings.json (outside the folders it snapshots) and English strings."""
    monkeypatch.setattr(i18n, "_settings_path", str(tmp_path_factory.mktemp("settings") / "settings.json"))
    i18n.set_lang("en", persist=False)
    yield


def snapshot(root) -> dict:
    """Every file (SHA-1) and folder under root; system clutter is left out (it may go with an emptied folder)."""
    import scan

    files, dirs = {}, []
    for here, ds, fs in os.walk(str(root)):
        dirs.append(os.path.relpath(here, str(root)))
        for f in fs:
            if scan.is_junk(f):
                continue
            p = os.path.join(here, f)
            with open(p, "rb") as fh:
                files[os.path.relpath(p, str(root))] = hashlib.sha1(fh.read()).hexdigest()
    return {"files": files, "dirs": sorted(dirs)}


def without_log(snap: dict) -> dict:
    files = {k: v for k, v in snap["files"].items() if not os.path.basename(k).startswith("organize_log.json")}
    return {"files": files, "dirs": snap["dirs"]}


def files_under(root) -> list[str]:
    out = []
    for here, ds, fs in os.walk(str(root)):
        for f in fs:
            out.append(os.path.relpath(os.path.join(here, f), str(root)).replace(os.sep, "/"))
    return sorted(out)


def build(root, dest=None, mode="move", prefs=None, heic_only=False, convert=None, dedupe=True, similar=False,
          overrides=None):
    """scan -> duplicates -> plan, exactly as the GUI and the CLI do it (main.make_plan)."""
    import main
    import prefs as prefs_mod

    p = prefs or prefs_mod.Prefs()
    conv = p.convert if convert is None else convert
    result, the_plan, _ = main.make_plan(str(root), str(dest or root), p, mode, heic_only, conv or heic_only,
                                         dedupe, similar, overrides=overrides)
    return result, the_plan


def by_name(the_plan, name: str):
    for item in the_plan.items:
        if any(m.name == name for m in item.members):
            return item
    raise KeyError(name)


@pytest.fixture
def sample_folder(tmp_path):
    folder = tmp_path / "in1"
    ms.make_all(str(folder))
    return folder


def copy_tree(src, dst) -> None:
    shutil.copytree(str(src), str(dst))
