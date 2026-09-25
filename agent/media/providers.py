"""Swappable image/video generation backends, chosen by IMAGE_PROVIDER / VIDEO_PROVIDER in
settings.env. Only Gemini images are implemented right now (you already have that connected).
Veo, ComfyUI and Wan can be added later as more classes here, behind the same interface,
without touching agent/social.py - ComfyUI/Wan specifically need a GPU server you control,
which this project doesn't set up, so they're not wired in yet."""
from __future__ import annotations


class ImageGenerator:
    def generate(self, prompt: str, aspect_ratio: str | None = None) -> bytes:
        raise NotImplementedError

    @property
    def unavailable(self) -> bool:
        """True when this backend can't make any more images this run (no key/quota left)."""
        return False


class GeminiImageGenerator(ImageGenerator):
    """Thin wrapper around the existing Gemini client (agent/writer.py), so image generation
    reuses the same multi-key rotation/failover that text writing already relies on."""
    def __init__(self, gemini):
        self.gemini = gemini

    def generate(self, prompt: str, aspect_ratio: str | None = None) -> bytes:
        return self.gemini.generate_image(prompt, aspect_ratio)

    @property
    def unavailable(self) -> bool:
        return self.gemini.images_exhausted


def build_image_generator(cfg, gemini) -> ImageGenerator:
    provider = cfg.image_provider
    if provider == "gemini":
        if not gemini:
            raise RuntimeError("IMAGE_PROVIDER=gemini but no GEMINI_API_KEY is set")
        return GeminiImageGenerator(gemini)
    raise RuntimeError(f"IMAGE_PROVIDER={provider!r} is not implemented yet (only 'gemini' is available so far)")
