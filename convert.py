"""HEIC (and optionally AVIF) to JPG.

The JPG keeps the whole EXIF block (date, camera, GPS) and the ICC profile.
Pixels are turned upright and Orientation is set to 1, because some viewers
ignore the tag and show the photo on its side. Only the first image of a
burst is converted. Runs in worker processes; each call is independent.
"""
from __future__ import annotations

import dataclasses
import io
import os

from longpath import fs

DEFAULT_QUALITY = 92
MIN_QUALITY, MAX_QUALITY = 80, 100
ORIENTATION = 0x0112


@dataclasses.dataclass
class Outcome:
    src: str
    dst: str
    ok: bool = False
    error: str = ""
    burst: bool = False
    rejected_tags: list[str] = dataclasses.field(default_factory=list)
    in_size: int = 0
    out_size: int = 0


def _register() -> None:
    import pillow_heif

    pillow_heif.register_heif_opener()


def _raw_exif(src: str, im) -> bytes:
    """The file's own EXIF (pillow-heif's copy has Orientation already set to 1; either is fine
    because the pixels come out upright and we write 1 anyway)."""
    data = im.info.get("exif") or b""
    if data.startswith(b"Exif\x00\x00"):
        data = data[6:]
    return data


def _clean_exif(raw: bytes, width: int, height: int) -> tuple[bytes, list[str]]:
    """EXIF for the JPG: Orientation 1, new pixel size, no embedded thumbnail. Tags piexif
    refuses to write are dropped one by one and reported."""
    import piexif

    if not raw:
        return b"", []
    try:
        data = piexif.load(b"Exif\x00\x00" + raw if not raw.startswith(b"Exif") else raw)
    except Exception:
        return b"", ["(unreadable EXIF)"]
    data["0th"][piexif.ImageIFD.Orientation] = 1
    data.setdefault("Exif", {})
    data["Exif"][piexif.ExifIFD.PixelXDimension] = width
    data["Exif"][piexif.ExifIFD.PixelYDimension] = height
    data["thumbnail"] = None
    data["1st"] = {}
    try:
        return piexif.dump(data), []
    except Exception:
        pass
    rejected = []
    for ifd in ("0th", "Exif", "GPS", "Interop"):
        for tag in list(data.get(ifd, {})):
            probe = {"0th": {}, "Exif": {}, "GPS": {}, "Interop": {}, "1st": {}, "thumbnail": None}
            probe[ifd] = {tag: data[ifd][tag]}
            try:
                piexif.dump(probe)
            except Exception:
                rejected.append(f"{ifd}:{piexif.TAGS.get(ifd, {}).get(tag, {}).get('name', hex(tag))}")
                del data[ifd][tag]
    try:
        return piexif.dump(data), rejected
    except Exception:
        return b"", rejected + ["(EXIF dropped)"]


def _to_srgb(im, icc: bytes):
    from PIL import ImageCms

    src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
    out = ImageCms.profileToProfile(im, src, srgb, outputMode="RGB")
    return out, srgb.tobytes()


def convert_file(src: str, dst: str, quality: int = DEFAULT_QUALITY, icc_mode: str = "keep") -> Outcome:
    """Write ``dst`` + ".part" (the caller renames it into place). Never raises."""
    from PIL import Image, ImageOps

    out = Outcome(src, dst)
    part = dst + ".part"
    try:
        _register()
        out.in_size = os.path.getsize(fs(src))
        with Image.open(fs(src)) as im:
            out.burst = getattr(im, "n_frames", 1) > 1
            raw = _raw_exif(src, im)
            icc = im.info.get("icc_profile") or b""
            im.seek(0)
            pic = ImageOps.exif_transpose(im)  # no-op unless the decoder left a turn to do
            if pic.mode not in ("RGB", "L"):
                pic = pic.convert("RGB")
            if icc and icc_mode == "srgb":
                pic, icc = _to_srgb(pic, icc)
            exif, out.rejected_tags = _clean_exif(raw, pic.width, pic.height)
            q = max(MIN_QUALITY, min(MAX_QUALITY, int(quality)))
            kwargs = {"quality": q}
            if exif:
                kwargs["exif"] = exif
            if icc:
                kwargs["icc_profile"] = icc
            pic.save(fs(part), "JPEG", **kwargs)
        st = os.stat(fs(src))
        os.utime(fs(part), ns=(st.st_atime_ns, st.st_mtime_ns))  # Explorer sorts the JPG next to its HEIC
        out.out_size = os.path.getsize(fs(part))
        out.ok = True
    except Exception as exc:  # damaged file, 10-bit we cannot read, image sequence...
        out.error = f"{exc.__class__.__name__}: {exc}"[:300]
        try:
            os.remove(fs(part))
        except OSError:
            pass
    return out


def avif_supported() -> bool:
    try:
        from PIL import features

        return bool(features.check("avif"))
    except Exception:
        return False
