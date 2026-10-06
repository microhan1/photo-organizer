"""Entry point for photo-organizer.

    python main.py                                  -> GUI
    python main.py D:\\Photos [--pattern P] [--dest D] [--copy] [--convert-heic] [--heic-only]
                   [--dedupe] [--similar] [--use-mtime] [--dry-run]
    python main.py D:\\Photos --undo               -> reverse the latest run
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
import sys

import dedupe
import i18n
import mover
import plan as plan_mod
import prefs as prefs_mod
import scan as scan_mod
import undo as undo_mod
from i18n import t

ARROW = chr(0x2192)


def _preselect_lang(argv: list[str]) -> str | None:
    """--lang has to be known before the parser is built, so help is translated."""
    for i, a in enumerate(argv):
        if a == "--lang" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--lang="):
            return a.split("=", 1)[1]
    return None


def _localize_argparse() -> None:
    """argparse's own labels go through gettext; route them to lang files."""
    table = {
        "usage: ": t("cli_usage"),
        "positional arguments": t("cli_positional"),
        "options": t("cli_options"),
        "show this help message and exit": t("cli_help"),
    }
    argparse._ = lambda s: table.get(s, s)  # type: ignore[attr-defined]


def build_parser() -> argparse.ArgumentParser:
    _localize_argparse()
    p = argparse.ArgumentParser(prog="photo-organizer", description=t("cli_desc"))
    p.add_argument("folder", nargs="?", help=t("cli_folder"))
    p.add_argument("--pattern", help=t("cli_pattern"))
    p.add_argument("--dest", help=t("cli_dest"))
    p.add_argument("--copy", action="store_true", help=t("cli_copy"))
    p.add_argument("--convert-heic", action="store_true", help=t("cli_convert_heic"))
    p.add_argument("--heic-only", action="store_true", help=t("cli_heic_only"))
    p.add_argument("--dedupe", action="store_true", help=t("cli_dedupe"))
    p.add_argument("--similar", action="store_true", help=t("cli_similar"))
    p.add_argument("--use-mtime", action="store_true", help=t("cli_use_mtime"))
    p.add_argument("--include-nodate", action="store_true", help=t("cli_include_nodate"))
    p.add_argument("--dry-run", action="store_true", help=t("cli_dry_run"))
    p.add_argument("--undo", action="store_true", help=t("cli_undo"))
    p.add_argument("--lang", choices=i18n.LANGS, help=t("cli_lang"))
    p.add_argument("--gui", action="store_true", help=t("cli_gui"))
    return p


def _rel(path: str, base: str) -> str:
    try:
        rel = os.path.relpath(path, base)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def status_label(item: plan_mod.Item) -> str:
    return t(f"status_{item.status}")


def basis_label(item: plan_mod.Item) -> str:
    return t(f"basis_{item.basis}") if item.basis else "-"


def run_undo(folder: str) -> int:
    folder = os.path.abspath(folder)
    logs = undo_mod.candidate_logs(folder)
    res = undo_mod.undo(undo_mod.find_log(folder), other_logs=logs)
    if res.blocked_by:
        first = res.blocked_by[0]
        print(t("msg_undo_blocked", time=first.time.replace("T", " "), id=first.id), file=sys.stderr)
        return 1
    if res.nothing:
        print(t("msg_no_log"))
        return 1
    if res.log_error:
        print(res.log_error, file=sys.stderr)
        return 1
    print(t("msg_undo_done", count=res.restored))
    for path, reason in res.skipped:
        print(t("msg_undo_skipped_item", path=path, reason=reason), file=sys.stderr)
    for path in res.trashed:
        print(t("msg_undo_trashed_item", path=path), file=sys.stderr)
    if res.log_unsaved:
        print(res.log_unsaved, file=sys.stderr)
    return 1 if res.skipped or res.log_unsaved else 0


def make_plan(root: str, dest: str, p: prefs_mod.Prefs, mode: str, heic_only: bool, convert_on: bool,
              dedupe_on: bool, similar_on: bool, progress=None, cancel=None, cache=None,
              overrides: plan_mod.Overrides | None = None, similar_map=None):
    """scan -> exact duplicates -> (similar) -> plan, the way the GUI and the CLI both do it.
    Returns (scan result, plan, similar map)."""
    opts = prefs_mod.options(p, root, dest, mode, heic_only=heic_only, convert_on=convert_on)
    result = scan_mod.scan(root, prefs_mod.special_folders(root, opts.dest), progress=progress, cancel=cancel,
                           cache=cache)
    first = plan_mod.group_items(result, opts, overrides) if (dedupe_on or similar_on) and not heic_only else []
    dupes = {}
    if dedupe_on and not heic_only:
        dupes = dedupe.find_exact(first, cancel=cancel)
    if similar_on and not heic_only and similar_map is None:
        similar_map = dedupe.find_similar(first, p.similar_threshold, cancel=cancel)
    return result, plan_mod.build(result, opts, dupes, similar_map or {}, overrides, grouped=first or None), \
        similar_map or {}


def print_plan(the_plan: plan_mod.Plan, root: str, dest: str) -> None:
    for item in the_plan.items:
        new = _rel(item.dst, dest) if item.dst else t("lbl_trash")
        mark = " " if item.acts else "-"
        when = item.when.strftime("%Y-%m-%d %H:%M") if item.when else "-"
        extra = "".join(f" +{m.ext}" for m in item.members[1:])
        print(f"{mark} [{status_label(item)}] {_rel(item.primary.path, root)}{extra}  {when} ({basis_label(item)})"
              f" {ARROW} {new}")
        if item.convert_dst:
            print(f"      {ARROW} {_rel(item.convert_dst, dest)}")
        if item.similar:
            hint = t("note_suggest_dup") if item.suggest_dup else ""
            print(f"      {t('status_similar')} #{item.similar} {hint}")


def run_cli(args: argparse.Namespace) -> int:
    root = os.path.abspath(args.folder)
    if not os.path.isdir(root):
        print(t("err_not_folder", path=args.folder), file=sys.stderr)
        return 2
    if args.undo:
        return run_undo(root)
    p = prefs_mod.load()
    if args.pattern:
        problem = plan_mod.validate_pattern(args.pattern)
        if problem:
            print(t(problem) + f": {args.pattern}", file=sys.stderr)
            return 2
        p.pattern = args.pattern
    if args.use_mtime:
        p.use_mtime = True
    if args.include_nodate:
        p.include_nodate = True
    dest = os.path.abspath(args.dest) if args.dest else root
    mode = plan_mod.COPY if args.copy else plan_mod.MOVE
    if mode == plan_mod.COPY and scan_mod.key_of(dest) == scan_mod.key_of(root):
        print(t("err_copy_in_place"), file=sys.stderr)
        return 2
    print(t("msg_scanning_path", path=root))
    if args.similar:
        print(t("msg_similar_started"))
    result, the_plan, _ = make_plan(root, dest, p, mode, args.heic_only, args.convert_heic or args.heic_only,
                                    args.dedupe or args.similar, args.similar)
    if result.cloud:
        print(t("msg_cloud_warning", count=len(result.cloud)), file=sys.stderr)
    if result.denied:
        print(t("msg_denied", count=len(result.denied)), file=sys.stderr)
    if not result.media:
        print(t("msg_no_media"))
        return 1
    print_plan(the_plan, root, dest)
    s = the_plan.summary()
    print(t("summary", **s))
    blocked = the_plan.blocked_groups()
    if blocked:
        print(t("msg_blocked_group"), file=sys.stderr)
        return 2
    if args.dry_run:
        print(t("msg_dry_run"))
        return 0
    need, free = mover.space_check(the_plan)
    if need > free:
        print(t("msg_no_space", need=human_size(need), free=human_size(free)), file=sys.stderr)
        return 1
    try:
        res = mover.execute(the_plan, progress=_progress, quality=p.quality, icc=p.icc)
    except OSError as exc:  # the log could not be written: nothing was moved
        print(t("err_log_write") + f" ({mover.log_path_for(the_plan.options.dest)}: {exc})", file=sys.stderr)
        return 1
    print()
    for path, reason in res.failed:
        print(t("msg_failed_item", path=mover.plain(path), reason=reason), file=sys.stderr)
    for path, text in res.notes:
        print(f"  {path}: {text}")
    print(t("msg_done", move=res.done, convert=res.converted))
    if res.converted:
        print(t("msg_size_change", before=human_size(res.bytes_in), after=human_size(res.bytes_out)))
    if res.log_path:
        print(t("msg_log_saved", path=res.log_path))
    return 1 if res.failed else 0


def _progress(done: int, total: int) -> None:
    if sys.stdout is not None and sys.stdout.isatty():
        print(f"\r{done}/{total}", end="", flush=True)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Windows consoles default to cp949; Chinese/Japanese names would crash print().
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    i18n.init(_preselect_lang(argv))
    args = build_parser().parse_args(argv)
    if args.lang:
        i18n.set_lang(args.lang, persist=False)
    # A windowed exe has no console: a folder dropped on it opens the GUI with that folder.
    headless = getattr(sys, "frozen", False) and sys.stdout is None
    if args.gui or headless or not args.folder:
        import gui

        gui.launch(args.folder)
        return 0
    try:
        return run_cli(args)
    except KeyboardInterrupt:
        print("\n" + t("msg_cancelled"))
        return 130


if __name__ == "__main__":
    multiprocessing.freeze_support()  # the exe's HEIC conversion workers start through here
    sys.exit(main())
