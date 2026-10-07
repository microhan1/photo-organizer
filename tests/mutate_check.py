"""Break one rule at a time in a copy of the program and check that the tests notice.

    python tests/mutate_check.py <work dir>

A suite that passes proves little until it fails on purpose (TrimPDF LESSONS 12).
Each mutation names the file, the exact text to replace, and the tests that must fail.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_TIMEOUT = 180  # seconds per mutation; longer means the mutation froze the program

MUTATIONS = [
    ("log saved before the first move", "mover.py",
     "        save_log(self.path, self.data)  # raises: the caller stops before touching any file\n", "",
     "tests/test_storage.py::test_log_that_cannot_be_written_moves_nothing"),
    ("mid-run log failure stops the run", "mover.py",
     "            except LogWriteError:\n                log_stopped = True\n                res.cancelled = True\n                break\n            tick()",
     "            except LogWriteError:\n                log_stopped = True\n            tick()",
     "tests/test_storage.py -k log_failing"),
    ("a file already at a name this run could give it stays", "plan.py",
     "    if _in_place_already(item, folder, stem, ctx):\n        return\n", "",
     "tests/test_long_names.py -k colliding"),
    ("a file never collides with itself", "plan.py", "        if tk in own:\n            continue\n", "",
     "tests/test_storage.py::test_same_name_same_date_numbered_or_duplicate_and_stable"),
    ("a pair member failing puts the other back", "mover.py",
     "                    move_file(dst, src)\n                    log.add(op=\"move\", src=dst, dst=src)",
     "                    pass",
     "tests/test_storage.py::test_pair_member_failing_puts_the_other_back"),
    ("camera clock reset prefers the name", "dates.py",
     "            if named:\n                return Resolved(named.when, name_basis, source)\n            if _in_range(when, years):\n                return Resolved(when, B_EXIF_SUSPECT, source)",
     "            if _in_range(when, years):\n                return Resolved(when, B_EXIF, source)",
     "tests/test_dates.py -k reset"),
    ("orientation reset to 1", "convert.py", "data[\"0th\"][piexif.ImageIFD.Orientation] = 1", "pass",
     "tests/test_formats.py -k upright"),
    ("cloud-only files skipped", "scan.py", "            if is_cloud_only(st):", "            if False:",
     "tests/test_storage.py::test_cloud_only_files_are_skipped_and_never_opened"),
    ("kept-by-hand is never a duplicate again", "plan.py", "    if item.status != DUP and not item.keep:",
     "    if item.status != DUP:", "tests/test_dedupe.py::test_exact_duplicate_kept_by_hand_is_not_moved"),
    ("KakaoTalk copy suggested", "dedupe.py", "    if kakao and len(kakao) < len(group):", "    if False:",
     "tests/test_dedupe.py -k kakaotalk"),
    ("drive pulled stops the run", "mover.py", "    return not os.path.isdir(fs(opts.root)) or not os.path.isdir(fs(opts.dest))",
     "    return False", "tests/test_storage.py::test_drive_pulled_mid_run_stops_and_can_be_undone"),
    ("undo deletes the converted JPG", "undo.py", "            elif kind in (\"copy\", \"convert\"):",
     "            elif kind == \"copy\":", "tests/test_storage.py::test_thousand_photos_organize_convert_and_undo_back_to_identical"),
    ("live photo pairs", "plan.py", "        if pairable and sum(1 for m in files if m.kind == \"image\") == 0:",
     "        if False:", "tests/test_formats.py::test_live_photo_pair_moves_together_and_only_the_heic_converts"),
    ("pHash rotations", "dedupe.py", "    return _bits(r0), _bits(r90), _bits(r180), _bits(r270)",
     "    return _bits(r0), _bits(r0), _bits(r0), _bits(r0)",
     "tests/test_dedupe.py::test_phash_survives_rotation_and_recompression"),
    ("run waits for the current plan", "gui.py",
     "        return self.plan is not None and self.plan_serial == self.serial and not self.busy and self.replan_job is None",
     "        return self.plan is not None and not self.busy",
     "tests/test_gui.py::test_run_waits_for_a_plan_made_with_the_current_settings"),
    ("renamed names read back as the original", "patterns.py",
     "    return stem[m.end():] if m and _to_date(m, (1, 9999)) else stem", "    return stem",
     "tests/test_cases.py -k rename"),
    ("junctions and symlinks are not entered", "scan.py", " and not e.is_symlink() and not e.is_junction()", "",
     "tests/test_cases.py -k junction"),
    ("copy again is quiet", "plan.py", "    if opts.mode == COPY and _already_copied(item, folder, stem, ctx):\n        return\n", "",
     "tests/test_cases.py -k copy"),
    ("No Date folder stays across languages", "plan.py",
     "        return [_nodate_folder_in_place(item, opts) or opts.nodate_name]", "        return [opts.nodate_name]",
     "tests/test_cases.py -k language"),
    ("leaving JPG does not date the pair", "plan.py",
     "        members = [m for m in members if m.kind != \"jpeg\"]", "        pass",
     "tests/test_cases.py -k leaves_as_duplicate"),
    ("{ext} follows the converted JPG", "plan.py", "        ext = \".jpg\"  # the HEIC leaves", "        pass  # the HEIC leaves",
     "tests/test_cases.py -k ext_pattern"),
    ("convert off when JPG pair is duplicate", "prefs.py", "    wants_convert = wants_convert and (heic_only or not p.jpg_pair_as_dupe)",
     "    pass", "tests/test_cases.py -k jpg_pair_as_duplicate"),
    ("summary toggle counted once (window froze)", "gui.py",
     "        numbers = sum(w.winfo_reqwidth() + 2 * px(4) + P for w in self.sum_line.winfo_children())",
     "        numbers = self.sum_line.winfo_reqwidth()",
     "tests/test_gui.py::test_summary_reflow_settles_at_every_width"),
    ("long paths", "longpath.py", "    if len(p) < LONG_PATH or p.startswith(", "    if True or p.startswith(",
     "tests/test_formats.py::test_long_source_path_is_reached_with_the_long_form"),  # never the \\?\ form
    ("NFC collision key", "plan.py", "    return os.path.normcase(unicodedata.normalize(\"NFC\", path))",
     "    return os.path.normcase(path)",
     "tests/test_formats.py::test_nfc_and_nfd_twins_in_one_target_get_numbered"),
]


def main() -> int:
    work = os.path.abspath(sys.argv[1])
    escaped = []
    for name, file, old, new, tests in MUTATIONS:
        copy = os.path.join(work, "m")
        if os.path.exists(copy):
            shutil.rmtree(copy)
        shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", "build", "dist", "__pycache__", "*.exe"))
        path = os.path.join(copy, file)
        src = open(path, encoding="utf-8").read()
        if old not in src:
            print(f"?? {name}: text not found in {file}")
            escaped.append(name)
            continue
        open(path, "w", encoding="utf-8").write(src.replace(old, new, 1))
        args = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests.split()]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PHOTO_ORGANIZER_SETTINGS=os.path.join(work, "s.json"))
        try:
            r = subprocess.run(args, cwd=copy, capture_output=True, text=True, encoding="utf-8", env=env,
                               timeout=TEST_TIMEOUT, stdin=subprocess.DEVNULL)
            caught = r.returncode != 0
        except subprocess.TimeoutExpired:
            caught = True  # a frozen window is caught too (the reflow bug hung, it did not fail)
        print(("caught " if caught else "ESCAPED") + f"  {name}")
        if not caught:
            escaped.append(name)
    shutil.rmtree(os.path.join(work, "m"), ignore_errors=False)
    print(f"{len(MUTATIONS) - len(escaped)}/{len(MUTATIONS)} caught")
    return 1 if escaped else 0


if __name__ == "__main__":
    sys.exit(main())
