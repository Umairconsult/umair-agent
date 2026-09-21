"""Optional: sends blog posts you APPROVED in the portal to your WordPress website.
Uses a WordPress "Application Password" (safe, revocable, no main password needed)."""
from __future__ import annotations
import json
import re
from urllib.parse import urlparse

import requests

from .logutil import log, safe_exc
from .portal import Portal


class WordPressError(Exception):
    pass


def norm_link(u: str) -> str:
    """https://www.Site.com/About/?x=1 -> site.com/about"""
    p = urlparse(u if "://" in u else "https://" + u)
    host = (p.netloc or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return host + (p.path.rstrip("/") or "")


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

    # ---- SEO fields (Rank Math), available after the small WPCode snippet is installed ----
    _SEO_KEYS = ("rank_math_title", "rank_math_description", "rank_math_focus_keyword")
    _seo_ok: bool | None = None
    _index: dict | None = None

    def seo_ready(self) -> bool:
        if self._seo_ok is None:
            try:
                r = self.s.get(self.base + "/pages", auth=self.auth, timeout=30, params={"per_page": 1, "context": "edit", "_fields": "id,meta"})
                meta = (r.json()[0].get("meta") if r.status_code == 200 and r.json() else None)
                self._seo_ok = isinstance(meta, dict) and "rank_math_description" in meta
            except (requests.RequestException, ValueError, IndexError, KeyError):
                self._seo_ok = False
        return self._seo_ok

    def _list(self, route: str) -> list[dict]:
        out: list[dict] = []
        for page in range(1, 6):
            r = self.s.get(f"{self.base}/{route}", auth=self.auth, timeout=40,
                           params={"per_page": 100, "page": page, "status": "publish", "_fields": "id,link,slug,title"})
            if r.status_code == 400:      # past the last page
                break
            if r.status_code != 200:
                raise WordPressError(f"could not list {route} (HTTP {r.status_code})")
            items = r.json()
            out += items
            if len(items) < 100:
                break
        return out

    def index(self) -> dict:
        """normalised link -> {'kind': 'page'|'post', 'id', 'title', 'link'}"""
        if self._index is None:
            idx = {}
            for route, kind in (("pages", "page"), ("posts", "post")):
                for it in self._list(route):
                    title = re.sub(r"<[^>]+>", "", (it.get("title") or {}).get("rendered", "")).strip()
                    idx[norm_link(it["link"])] = {"kind": kind, "id": int(it["id"]), "title": title, "link": it["link"]}
            self._index = idx
        return self._index

    def set_seo(self, kind: str, obj_id: int, title: str, description: str, keyword: str = "") -> None:
        route = "pages" if kind == "page" else "posts"
        meta = {"rank_math_title": title, "rank_math_description": description}
        if keyword:
            meta["rank_math_focus_keyword"] = keyword
        r = self.s.post(f"{self.base}/{route}/{obj_id}", auth=self.auth, timeout=60, json={"meta": meta})
        if r.status_code in (401, 403):
            raise WordPressError("this WordPress user is not allowed to edit that page")
        if r.status_code >= 400:
            raise WordPressError(f"WordPress refused the SEO update (HTTP {r.status_code})")
        got = (r.json().get("meta") or {})
        if got.get("rank_math_description") != description or got.get("rank_math_title") != title:
            raise WordPressError("WordPress accepted the request but did not save the SEO fields (is the WPCode snippet active?)")

    def create_post(self, title: str, html: str, slug: str, excerpt: str, seo: dict | None = None) -> tuple[int, str]:
        body = {"title": title, "content": html, "slug": slug, "excerpt": excerpt, "status": self.mode}
        if seo and self.seo_ready():
            body["meta"] = {k: v for k, v in seo.items() if k in self._SEO_KEYS and v}
        r = self.s.post(self.base + "/posts", auth=self.auth, timeout=60, json=body)
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
            try:
                kw = (json.loads(it.get("extras_json") or "{}").get("keywords") or [""])[0]
            except (ValueError, AttributeError):
                kw = ""
            seo = {"rank_math_title": it["title"][:60], "rank_math_description": it.get("meta_description") or "", "rank_math_focus_keyword": kw}
            pid, link = wp.create_post(it["title"], it.get("body_html") or "", it.get("slug") or "", it.get("excerpt") or "", seo)
        except (WordPressError, requests.RequestException) as e:
            portal.log("error", f"Could not send a blog post to WordPress: {safe_exc(e, 150)}")
            break
        portal.content_update(id=int(it["id"]), status="published" if wp.mode == "publish" else "sent", wp_post_id=pid, published_url=link)
        portal.log("blog", f"Sent blog post to your website as a {'live post' if wp.mode == 'publish' else 'draft'}: {it['title']}")
        out["sent"] += 1
    return out



class WordPressBridge:
    """Talks to the small UmairConsult bridge snippet in WPCode (a secret key, no WordPress password needed)."""
    _SHORT = {"rank_math_title": "title", "rank_math_description": "description", "rank_math_focus_keyword": "keyword"}

    def __init__(self, url: str, key: str, mode: str = "publish"):
        self.root = url.rstrip("/") + "/wp-json/umairconsult-agent/v1"
        self.key = key
        self.mode = mode
        self.rank_math = False
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "UmairConsultAgent/1.0"})
        self._ok: bool | None = None
        self._index: dict | None = None

    def _call(self, method: str, route: str, **kw) -> dict:
        r = self.s.request(method, self.root + route, headers={"X-Agent-Key": self.key}, timeout=60, **kw)
        if r.status_code == 403:
            raise WordPressError("the bridge refused the key (WP_AGENT_KEY does not match the key inside the WordPress snippet)")
        if r.status_code == 404:
            raise WordPressError("bridge not found on your website - is the WPCode snippet saved and switched to Active?")
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", "")
            except ValueError:
                msg = ""
            raise WordPressError(f"WordPress bridge error (HTTP {r.status_code}) {msg}".strip())
        try:
            return r.json()
        except ValueError:
            raise WordPressError("the bridge sent an unreadable answer")

    def ping(self) -> str:
        d = self._call("GET", "/ping")
        self.rank_math = bool(d.get("rank_math"))
        return str(d.get("site", ""))

    def seo_ready(self) -> bool:
        if self._ok is None:
            try:
                self.ping()
                self._ok = True
            except (WordPressError, requests.RequestException):
                self._ok = False
        return self._ok

    def index(self) -> dict:
        if self._index is None:
            d = self._call("GET", "/pages")
            self._index = {norm_link(i["link"]): {"kind": i["kind"], "id": int(i["id"]), "title": i.get("title", ""), "link": i["link"]}
                           for i in d.get("items", [])}
        return self._index

    def set_seo(self, kind: str, obj_id: int, title: str, description: str, keyword: str = "") -> None:
        d = self._call("POST", "/seo", json={"id": obj_id, "title": title, "description": description, "keyword": keyword})
        got = d.get("seo") or {}
        strip = lambda x: re.sub(r"<[^>]+>", "", x or "").strip()
        if strip(got.get("title")) != strip(title) or strip(got.get("description")) != strip(description):
            raise WordPressError("WordPress accepted the request but did not keep the SEO fields")

    def create_post(self, title: str, html: str, slug: str, excerpt: str, seo: dict | None = None) -> tuple[int, str]:
        body = {"title": title, "content": html, "slug": slug, "excerpt": excerpt, "status": self.mode}
        if seo:
            body["seo"] = {self._SHORT.get(k, k): v for k, v in seo.items() if v}
        d = self._call("POST", "/post", json=body)
        return int(d["id"]), str(d.get("link", ""))
