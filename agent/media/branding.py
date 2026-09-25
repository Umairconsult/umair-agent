"""Puts your real logo file onto a generated image, deterministically - never by asking an AI
model to draw a logo. Position, size, margin and opacity all come from settings.env, not
hard-coded, so you can adjust the look without touching code."""
from __future__ import annotations
from io import BytesIO
from pathlib import Path

POSITIONS = ("bottom_right", "bottom_left", "top_right", "top_left", "center")


class BrandingError(Exception):
    pass


def _resample():
    from PIL import Image
    try:
        return Image.Resampling.LANCZOS
    except AttributeError:
        return Image.LANCZOS  # older Pillow


def fit_to_size(image_bytes: bytes, size: tuple[int, int]) -> bytes:
    """Centre-crops (never stretches) and resizes the picture to EXACTLY the platform's size, and
    returns PNG bytes. AI image tools return their own sizes (e.g. 1024x1024), which are not the
    sizes Instagram / LinkedIn / Facebook want, so this makes every image the right shape."""
    try:
        from PIL import Image
    except ImportError as e:
        raise BrandingError("the 'Pillow' package isn't installed (see requirements.txt)") from e
    try:
        img = Image.open(BytesIO(image_bytes)).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise BrandingError(f"generated image could not be opened ({type(e).__name__})") from e
    tw, th = size
    w, h = img.size
    if w < 2 or h < 2:
        raise BrandingError("generated image is empty")
    target_ratio, ratio = tw / th, w / h
    if ratio > target_ratio:            # too wide: trim the sides
        nw = max(1, round(h * target_ratio))
        left = (w - nw) // 2
        img = img.crop((left, 0, left + nw, h))
    elif ratio < target_ratio:          # too tall: trim top and bottom
        nh = max(1, round(w / target_ratio))
        top = (h - nh) // 2
        img = img.crop((0, top, w, top + nh))
    img = img.resize((tw, th), _resample())
    out = BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def apply_logo(image_bytes: bytes, logo_path: str, position: str = "bottom_right",
               margin: int = 40, width: int = 180, opacity: float = 0.90) -> bytes:
    """Returns new PNG bytes with the logo composited on top of the generated image."""
    try:
        from PIL import Image
    except ImportError as e:
        raise BrandingError("the 'Pillow' package isn't installed (see requirements.txt)") from e

    try:
        base = Image.open(BytesIO(image_bytes)).convert("RGBA")
    except Exception as e:  # noqa: BLE001 - any bad/corrupt image data
        raise BrandingError(f"generated image could not be opened ({type(e).__name__})") from e

    try:
        path = Path(logo_path)
        if not path.is_file():                      # also look next to the project (works from any folder)
            alt = Path(__file__).resolve().parent.parent.parent / logo_path
            path = alt if alt.is_file() else path
        logo = Image.open(path).convert("RGBA")
    except FileNotFoundError as e:
        raise BrandingError(f"logo file not found at '{logo_path}' - add your logo image to the repo at that "
                            f"path (or change LOGO_PATH in settings.env to match where you put it)") from e
    except Exception as e:  # noqa: BLE001
        raise BrandingError(f"logo file could not be opened ({type(e).__name__})") from e

    resample = _resample()
    width = max(1, min(width, base.width // 2))       # never let the logo swallow a small image
    margin = max(0, margin)
    ratio = width / logo.width
    logo = logo.resize((width, max(1, round(logo.height * ratio))), resample)

    if opacity < 1.0:
        alpha = logo.split()[3].point(lambda p: int(p * max(0.0, min(1.0, opacity))))
        logo.putalpha(alpha)

    lw, lh = logo.size
    bw, bh = base.size
    xy_by_position = {
        "bottom_right": (bw - lw - margin, bh - lh - margin),
        "bottom_left": (margin, bh - lh - margin),
        "top_right": (bw - lw - margin, margin),
        "top_left": (margin, margin),
        "center": ((bw - lw) // 2, (bh - lh) // 2),
    }
    xy = xy_by_position.get(position, xy_by_position["bottom_right"])
    base.paste(logo, xy, logo)  # logo's own alpha channel used as the blend mask

    out = BytesIO()
    base.convert("RGB").save(out, format="PNG")
    return out.getvalue()
