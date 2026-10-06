"""Make the sample files in samples/ (all drawn here: no third-party photos).

    python samples/make_samples.py

Also imported by the tests for its builders (photo, heic, video).
"""
from __future__ import annotations

import os
import random
import struct
import sys

import piexif
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
MAC_EPOCH_OFFSET = 2082844800


def picture(size: tuple[int, int] = (320, 240), seed: int = 1) -> Image.Image:
    """A made-up "photo": a sky gradient with a few shapes, different for every seed."""
    rnd = random.Random(seed)
    w, h = size
    im = Image.new("RGB", size)
    top = (rnd.randint(40, 120), rnd.randint(90, 160), rnd.randint(170, 240))
    for y in range(h):
        k = y / max(h - 1, 1)
        im.paste(tuple(int(c * (1 - k) + 230 * k) for c in top), (0, y, w, y + 1))
    d = ImageDraw.Draw(im)
    for _ in range(6):
        x0, y0 = rnd.randint(0, max(0, w - 20)), rnd.randint(h // 3, max(h // 3, h - 20))  # tiny sizes too
        x1, y1 = x0 + rnd.randint(w // 10, w // 3 + 1), y0 + rnd.randint(h // 10, h // 3 + 1)
        d.ellipse((x0, y0, x1, y1), fill=(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)))
    d.rectangle((w // 10, h // 10, w // 10 + w // 5, h // 10 + h // 8), fill=(250, 250, 240))
    return im


def exif_bytes(taken: str | None = None, orientation: int | None = None, make: str = "", model: str = "",
               gps: bool = False, digitized: str | None = None, datetime_: str | None = None) -> bytes:
    zeroth, exif, gps_ifd = {}, {}, {}
    if orientation:
        zeroth[piexif.ImageIFD.Orientation] = orientation
    if make:
        zeroth[piexif.ImageIFD.Make] = make.encode()
    if model:
        zeroth[piexif.ImageIFD.Model] = model.encode()
    if datetime_ is not None:
        zeroth[piexif.ImageIFD.DateTime] = datetime_.encode()
    if taken is not None:
        exif[piexif.ExifIFD.DateTimeOriginal] = taken.encode()
    if digitized is not None:
        exif[piexif.ExifIFD.DateTimeDigitized] = digitized.encode()
    if gps:
        gps_ifd = {piexif.GPSIFD.GPSLatitudeRef: b"N", piexif.GPSIFD.GPSLatitude: ((37, 1), (33, 1), (59, 1)),
                   piexif.GPSIFD.GPSLongitudeRef: b"E", piexif.GPSIFD.GPSLongitude: ((126, 1), (58, 1), (40, 1))}
    return piexif.dump({"0th": zeroth, "Exif": exif, "GPS": gps_ifd})


def photo(path: str, fmt: str = "JPEG", exif: bytes | None = None, size=(320, 240), seed: int = 1,
          quality: int = 90, image: Image.Image | None = None) -> str:
    """Write a picture as JPEG / PNG / TIFF / HEIF (fmt is the Pillow format name)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    im = image or picture(size, seed)
    kwargs = {}
    if exif:
        kwargs["exif"] = exif
    if fmt == "HEIF":
        import pillow_heif

        pillow_heif.register_heif_opener()
        kwargs["quality"] = quality
    elif fmt == "JPEG":
        kwargs["quality"] = quality
    im.save(path, format=fmt, **kwargs)
    return path


# ------------------------------------------------------------------ video atoms
def atom(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def mvhd(utc_seconds_since_1970: int | None, version: int = 0) -> bytes:
    secs = 0 if utc_seconds_since_1970 is None else utc_seconds_since_1970 + MAC_EPOCH_OFFSET
    if version == 1:
        body = bytes([1, 0, 0, 0]) + struct.pack(">QQIQ", secs, secs, 600, 0)
    else:
        body = bytes(4) + struct.pack(">IIII", secs, secs, 600, 0)
    return atom(b"mvhd", body + bytes(80))


def qt_meta_creationdate(text: str) -> bytes:
    """moov/meta in the QuickTime layout iPhones write (hdlr, keys, ilst)."""
    key = b"com.apple.quicktime.creationdate"
    hdlr = atom(b"hdlr", bytes(8) + b"mdta" + bytes(12) + b"\x00")
    keys = atom(b"keys", bytes(4) + struct.pack(">I", 1) + struct.pack(">I", 8 + len(key)) + b"mdta" + key)
    data = atom(b"data", struct.pack(">I", 1) + bytes(4) + text.encode())
    ilst = atom(b"ilst", atom(struct.pack(">I", 1), data))
    return atom(b"meta", hdlr + keys + ilst)


def udta_day(text: str) -> bytes:
    raw = text.encode()
    return atom(b"udta", atom(b"\xa9day", struct.pack(">HH", len(raw), 0x15C7) + raw))


def video(path: str, utc: int | None = None, local: str | None = None, day: str | None = None,
          brand: bytes = b"qt  ", mvhd_version: int = 0, payload: int = 2048) -> str:
    """A minimal MOV/MP4: only the boxes a date reader looks at, plus some media bytes."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    moov = mvhd(utc, mvhd_version)
    if local:
        moov += qt_meta_creationdate(local)
    if day:
        moov += udta_day(day)
    data = atom(b"ftyp", brand + bytes(4) + brand) + atom(b"mdat", bytes(random.Random(utc or 0).randbytes(payload)))
    data += atom(b"moov", moov)  # moov at the end, as many phones write it
    with open(path, "wb") as f:
        f.write(data)
    return path


def make_all(folder: str = HERE) -> list[str]:
    out = []
    j = lambda *p: os.path.join(folder, *p)  # noqa: E731
    out.append(photo(j("IMG_0001.HEIC"), "HEIF", exif_bytes("2024:03:15 12:34:56", orientation=6, make="Apple",
                                                            model="iPhone 15", gps=True), seed=1))
    out.append(video(j("IMG_0001.MOV"), utc=1710473696, local="2024-03-15T12:34:56+0900"))  # live photo
    with open(j("IMG_0001.AAE"), "wb") as f:
        f.write(b'<?xml version="1.0"?><plist version="1.0"><dict/></plist>')
    out.append(j("IMG_0001.AAE"))
    out.append(photo(j("IMG_1234.jpg"), seed=2))  # no EXIF, no date in the name
    out.append(photo(j("KakaoTalk_20240316_083000123.jpg"), seed=3))
    out.append(photo(j("KakaoTalk_Photo_2024-03-17-19-20-21.jpeg"), seed=4))
    out.append(photo(j("Screenshot_20240318_101112.png"), "PNG", seed=5))
    out.append(photo(j("스크린샷 2024-03-19 090807.png"), "PNG", seed=6))
    out.append(photo(j("20240320_141516.jpg"), exif=exif_bytes("2024:03:20 14:15:16", make="samsung",
                                                                 model="SM-S921N"), seed=7))
    out.append(photo(j("PXL_20240321_101010123.jpg"), seed=8))
    out.append(photo(j("IMG-20240322-WA0001.jpg"), seed=9))
    out.append(video(j("VID_20240323_181920.mp4"), utc=1711185560, brand=b"isom"))
    with open(j("empty.jpg"), "wb"):
        pass
    out.append(j("empty.jpg"))
    broken = photo(j("broken_exif.jpg"), exif=exif_bytes("2024:03:24 10:00:00"), seed=10)
    with open(broken, "r+b") as f:  # damage the TIFF header inside the EXIF block
        data = f.read()
        pos = data.find(b"Exif\x00\x00") + 6
        f.seek(pos)
        f.write(b"XX\x00\x00\xff\xff\xff\xff")
    out.append(broken)
    return out


if __name__ == "__main__":
    files = make_all(sys.argv[1] if len(sys.argv) > 1 else HERE)
    for p in files:
        print(os.path.basename(p).encode("ascii", "backslashreplace").decode(), os.path.getsize(p))
