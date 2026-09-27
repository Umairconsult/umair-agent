"""Swappable image/video generation backends, chosen by IMAGE_PROVIDER / VIDEO_PROVIDER in
settings.env. Gemini and Pollinations images are implemented (pick one with IMAGE_PROVIDER).
Veo, ComfyUI and Wan can be added later as more classes here, behind the same interface,
without touching agent/social.py - ComfyUI/Wan specifically need a GPU server you control,
which this project doesn't set up, so they're not wired in yet."""
from __future__ import annotations
import time
import urllib.parse

import requests


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


class PollinationsImageGenerator(ImageGenerator):
    """https://pollinations.ai - a free, no-signup, no-API-key image generation endpoint. No
    billing account needed anywhere, unlike Gemini's image models. The anonymous tier is rate
    limited (about one request per 15 seconds) and may add a small watermark; both go away if
    you later register a free account at https://auth.pollinations.ai and set
    POLLINATIONS_TOKEN, but this works with nothing set at all."""
    BASE = "https://image.pollinations.ai/prompt/"
    MIN_GAP = 17.0   # a little over the documented ~15s anonymous limit, to be safe

    def __init__(self, token: str = ""):
        self.token = token
        self._last_call = 0.0

    def _sleep_gap(self) -> None:
        wait = self._last_call + self.MIN_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def generate(self, prompt: str, aspect_ratio: str | None = None) -> bytes:
        # A rough pixel size just tells Pollinations what shape to aim for - the agent's own
        # fit_to_size() step afterwards still crops/resizes to the platform's exact dimensions,
        # so this only needs to be approximately right, not exact.
        size_by_aspect = {"1:1": (1024, 1024), "16:9": (1280, 720), "9:16": (720, 1280)}
        w, h = size_by_aspect.get(aspect_ratio or "1:1", (1024, 1024))
        url = self.BASE + urllib.parse.quote(prompt, safe="") + f"?width={w}&height={h}&nologo=true"
        if self.token:
            url += "&token=" + urllib.parse.quote(self.token, safe="")

        last_err = "unknown error"
        for attempt in range(3):
            self._sleep_gap()
            try:
                r = requests.get(url, timeout=90)
            except requests.RequestException as e:
                last_err = f"{type(e).__name__}: {e}"
                continue
            if r.status_code == 200 and r.content:
                return r.content
            if r.status_code == 429:
                last_err = "rate limited (HTTP 429) - waiting a bit longer and retrying"
                time.sleep(self.MIN_GAP)
                continue
            last_err = f"HTTP {r.status_code}"
        raise RuntimeError(f"Pollinations did not return an image ({last_err})")


def build_image_generator(cfg, gemini) -> ImageGenerator:
    provider = cfg.image_provider
    if provider == "gemini":
        if not gemini:
            raise RuntimeError("IMAGE_PROVIDER=gemini but no GEMINI_API_KEY is set")
        return GeminiImageGenerator(gemini)
    if provider == "pollinations":
        return PollinationsImageGenerator(cfg.get("POLLINATIONS_TOKEN"))
    raise RuntimeError(f"IMAGE_PROVIDER={provider!r} is not implemented yet (only 'gemini' and 'pollinations' are available so far)")
