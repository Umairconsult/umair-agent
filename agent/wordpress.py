"""Optional: sends blog posts you APPROVED in the portal to your WordPress website.
Uses a WordPress "Application Password" (safe, revocable, no main password needed)."""
from __future__ import annotations
import requests

from .logutil import log, safe_exc
from .portal import Portal


class WordPressError(Exception):
    pass


class WordPress:
    def __init__(self, url: str, user: str, app_password: str, mode: str = "draft"):
        self.base = url.rstrip("/") + "/wp-json/wp/v2"
        self.root = url.rstrip("/") + "/wp-json/"
        self.auth = (user, app_password.replace("\u00a0", " "))
        self.mode = mode
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "UmairConsultAgent/1.0"})

    def ping(self) -> str:
        r = self.s.get(self.base + "/users/me", auth=self.auth, timeout=25)
        if r.status_code == 401:
            raise WordPressError("WordPress rejected the username or application password")
        if r.status_code == 404:
            raise WordPressError("WordPress REST API not found (check WP_URL)")
        r.raise_for_status()
        return r.json().get("name", "")

    def create_post(self, title: str, html: str, slug: str, excerpt: str) -> tuple[int, str]:
        r = self.s.post(self.base + "/posts", auth=self.auth, timeout=60,
                        json={"title": title, "content": html, "slug": slug, "excerpt": excerpt, "status": self.mode})
        if r.status_code in (401, 403):
            raise WordPressError("This WordPress user is not allowed to create posts (needs Editor or Administrator)")
        if r.status_code >= 400:
            raise WordPressError(f"WordPress refused the post (HTTP {r.status_code})")
        d = r.json()
        return int(d["id"]), str(d.get("link", ""))


def publish_approved(cfg, portal: Portal, wp: WordPress) -> dict:
    out = {"sent": 0}
    for it in portal.content_list(status="approved", include_body=True, limit=20):
        if it.get("wp_post_id"):
            continue
        try:
            pid, link = wp.create_post(it["title"], it.get("body_html") or "", it.get("slug") or "", it.get("excerpt") or "")
        except (WordPressError, requests.RequestException) as e:
            portal.log("error", f"Could not send a blog post to WordPress: {safe_exc(e, 150)}")
            break
        portal.content_update(id=int(it["id"]), status="published" if wp.mode == "publish" else "sent", wp_post_id=pid, published_url=link)
        portal.log("blog", f"Sent blog post to your website as a {'live post' if wp.mode == 'publish' else 'draft'}: {it['title']}")
        out["sent"] += 1
    return out
