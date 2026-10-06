"""Design system constants: colours, fonts, spacing (PRD "디자인 시스템").

ttk widgets take their look from sv-ttk (Sun Valley); the plain tk widgets
(drop area, bands) and the table's status colours use the values here.
"""
from __future__ import annotations

import sys

LIGHT = {
    "bg": "#FAFAFA", "surface": "#FFFFFF", "border": "#E0E0E0", "text": "#1F2328", "muted": "#6B7280",
    "accent": "#2563EB", "drop_bg": "#F3F6FD", "ok_band": "#E8F6EE", "err_band": "#FDECEC", "warn": "#B45309",
}
DARK = {
    "bg": "#1E1E1E", "surface": "#2B2B2B", "border": "#3C3C3C", "text": "#E6E6E6", "muted": "#9CA3AF",
    "accent": "#2563EB", "drop_bg": "#242a36", "ok_band": "#16301F", "err_band": "#3A1D1D", "warn": "#F59E0B",
}
# table status: text + colour, never colour alone ("same" is only grey text)
STATUS_COLORS = {"ok": "#16A34A", "convert": "#2563EB", "nodate": "#9CA3AF", "dup": "#D97706",
                 "conflict": "#DC2626", "same": "#9CA3AF", "similar": "#D97706"}
OK_TEXT, ERR_TEXT = "#16A34A", "#DC2626"

PAD_OUT = 16  # window edge
PAD = 8  # between elements
PAD_CELL = 6  # inside table cells (rounded to the row height below)
ROW_HEIGHT = 32  # 10 pt text + 2 x cell padding, on the 8 px grid
ROW_HEIGHT_THUMB = 56  # 48 px thumbnail + 8
THUMB = 48
WINDOW = (1100, 760)
WINDOW_MIN = (960, 640)
BODY_PT = 10
NUMBER_PT = 14

# UI font per language; the table always uses Malgun Gothic, which has Hangul, kana and hanzi
UI_FONTS = {"ko": ("맑은 고딕", "Malgun Gothic"), "en": ("Segoe UI",), "zh-CN": ("Microsoft YaHei UI", "Microsoft YaHei"),
            "ja": ("Yu Gothic UI", "Meiryo UI", "Meiryo")}
TABLE_FONTS = ("맑은 고딕", "Malgun Gothic")
FALLBACK_FONTS = ("Segoe UI",)


SCALE = 1.0  # screen pixels per design pixel (1.5 at 150 % Windows scaling); set by the GUI at start


def set_scale(pixels_per_inch: float) -> None:
    """Sizes in this file are design pixels at 96 dpi. A DPI-aware window on a 150 % screen
    has 144 pixels per inch: windows, padding and row heights grow with it, as fonts do."""
    global SCALE
    SCALE = max(1.0, pixels_per_inch / 96.0)


def px(n: float) -> int:
    return int(round(n * SCALE))


def pick_font(families: set[str], candidates: tuple[str, ...]) -> str:
    for name in (*candidates, *FALLBACK_FONTS):
        if name in families:
            return name
    return "TkDefaultFont"


def system_prefers_dark() -> bool:
    """Windows "app mode" setting (Settings > Personalization > Colors)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def palette(theme: str) -> dict[str, str]:
    dark = theme == "dark" or (theme == "system" and system_prefers_dark())
    return DARK if dark else LIGHT


def is_dark(theme: str) -> bool:
    return palette(theme) is DARK
