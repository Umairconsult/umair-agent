"""Automated checks on a generated (and branded) image before it's sent to Drive/portal for
human review. If a check fails, the caller should count it as a failure for that platform
today rather than sending broken media to the review queue."""
from __future__ import annotations
from io import BytesIO

MIN_BYTES = 4_000        # smaller than this is almost certainly a broken/blank image
MAX_BYTES = 15_000_000   # generous ceiling; catches a runaway/garbage response


class QualityError(Exception):
    pass


def check_image(image_bytes: bytes, expected_size: tuple[int, int] | None = None,
                tolerance: float = 0.02) -> None:
    """Raises QualityError with a plain-English reason if the image fails a check.
    Returns nothing if everything looks fine."""
    if not image_bytes or len(image_bytes) < MIN_BYTES:
        raise QualityError("image file is empty or too small - likely a failed generation")
    if len(image_bytes) > MAX_BYTES:
        raise QualityError(f"image file is unexpectedly large ({len(image_bytes)} bytes)")
    try:
        from PIL import Image
    except ImportError as e:
        raise QualityError("the 'Pillow' package isn't installed (see requirements.txt)") from e
    try:
        img = Image.open(BytesIO(image_bytes))
        img.verify()
        img = Image.open(BytesIO(image_bytes))  # re-open: verify() leaves the handle unusable
        w, h = img.size
    except Exception as e:  # noqa: BLE001 - any corrupt/unreadable image
        raise QualityError(f"image file is corrupted or unreadable ({type(e).__name__})") from e
    if w < 200 or h < 200:
        raise QualityError(f"image is suspiciously small ({w}x{h})")
    try:
        from PIL import ImageStat
        spread = ImageStat.Stat(img.convert("L")).stddev[0]
    except Exception:  # noqa: BLE001 - this extra check must never break a good image
        spread = 99.0
    if spread < 3:
        raise QualityError("image looks blank (one flat colour) - likely a failed generation")
    if expected_size:
        ew, eh = expected_size
        if abs(w - ew) > ew * tolerance or abs(h - eh) > eh * tolerance:
            raise QualityError(f"image came back {w}x{h}, expected about {ew}x{eh} for this platform")
