"""Overture Maps: a free, open dataset of 60+ million places (names, websites, phones, emails, socials).
No scraping and no account. We read only the small area we need, straight from the public files."""
from __future__ import annotations
import math
import re
from urllib.parse import urlparse

import requests

from .web import clean_email, normalize_phone

STAC_CATALOG = "https://stac.overturemaps.org/catalog.json"
S3_GLOB = "s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"


class OvertureError(Exception):
    pass


def bbox_for(lat: float, lon: float, radius_m: int):
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * max(0.2, math.cos(math.radians(lat))))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat   # xmin, ymin, xmax, ymax


def _first(seq):
    for x in seq or []:
        if x and str(x).strip():
            return str(x).strip()
    return ""


def _social(urls, host_part: str, need_path: str = "") -> str:
    for u in urls or []:
        try:
            p = urlparse(u if "://" in u else "https://" + u)
        except ValueError:
            continue
        if host_part in (p.netloc or "").lower() and (not need_path or p.path.startswith(need_path)) and p.path.strip("/"):
            return "https://" + p.netloc.replace("m.", "", 1) + p.path.rstrip("/")
    return ""


class Overture:
    def __init__(self, local_glob: str | None = None):
        self.local = local_glob            # tests only: a local parquet file pattern
        self.con = None
        self.release = None
        self._stac_ready = False
        self._cols = None

    # ------------------------------------------------------------ setup
    def _connect(self):
        if self.con is not None:
            return self.con
        try:
            import duckdb
        except ImportError as e:
            raise OvertureError("duckdb is not installed") from e
        self.con = duckdb.connect()
        self.con.execute("SET threads=4")
        self.con.execute("SET memory_limit='1500MB'")
        if not self.local:
            self.con.execute("INSTALL httpfs")
            self.con.execute("LOAD httpfs")
            self.con.execute("SET s3_region='us-west-2'")
            for stmt in ("SET http_timeout=90000", "SET http_retries=3"):
                try:
                    self.con.execute(stmt)
                except Exception:  # noqa: BLE001 - older/newer DuckDB may not know a setting
                    pass
        return self.con

    def latest_release(self) -> str:
        if self.release:
            return self.release
        try:
            r = requests.get(STAC_CATALOG, timeout=25)
            r.raise_for_status()
            rel = r.json().get("latest")
        except (requests.RequestException, ValueError) as e:
            raise OvertureError("could not read the Overture release list") from e
        if not rel:
            raise OvertureError("Overture release list has no 'latest'")
        self.release = rel
        return rel

    def _files_for(self, bbox) -> list[str] | str:
        """The few data files that cover this area (or the whole-folder pattern as a fallback)."""
        if self.local:
            return self.local
        rel = self.latest_release()
        con = self._connect()
        try:
            if not self._stac_ready:
                con.execute(f"CREATE OR REPLACE TEMP TABLE stac AS SELECT bbox, assets.aws.alternate.s3.href AS href "
                            f"FROM read_parquet('https://stac.overturemaps.org/{rel}/collections.parquet') "
                            f"WHERE collection = 'place' AND type = 'Feature'")
                self._stac_ready = True
            xmin, ymin, xmax, ymax = bbox
            rows = con.execute("SELECT href FROM stac WHERE bbox.xmin < ? AND bbox.xmax > ? AND bbox.ymin < ? AND bbox.ymax > ?",
                               [xmax, xmin, ymax, ymin]).fetchall()
            files = [r[0] for r in rows if r[0]]
            if files:
                return files
        except Exception:  # noqa: BLE001 - fall back to reading the folder
            self._stac_ready = False
        return S3_GLOB.format(release=rel)

    def _columns(self, source) -> set[str]:
        if self._cols is None:
            con = self._connect()
            src = source if isinstance(source, str) else source[0]
            rows = con.execute("DESCRIBE SELECT * FROM read_parquet(?) LIMIT 0", [src]).fetchall()
            self._cols = {r[0] for r in rows}
        return self._cols

    # ------------------------------------------------------------ search
    def search(self, lat: float, lon: float, radius_m: int, keywords: list[str], city: str, limit: int = 300) -> list[dict]:
        if not keywords:
            return []
        bbox = bbox_for(lat, lon, radius_m)
        con = self._connect()
        try:
            source = self._files_for(bbox)
            cols = self._columns(source)
            cat_parts = [f"lower(CAST({c} AS VARCHAR))" for c in ("taxonomy.primary", "categories.primary", "basic_category")
                         if c.split(".")[0] in cols]
            if not cat_parts:
                raise OvertureError("Overture data has no category column")
            cat = "coalesce(" + ", ".join(cat_parts) + ", '')"
            like = " OR ".join(["%s LIKE ?" % cat] * len(keywords))
            extra = []
            if "confidence" in cols:
                extra.append("(confidence IS NULL OR confidence >= 0.55)")
            if "operating_status" in cols:
                extra.append("(operating_status IS NULL OR operating_status NOT LIKE 'permanently%')")
            sel_status = "operating_status" if "operating_status" in cols else "NULL"
            sql = f"""
                SELECT names.primary AS name, {cat} AS category, websites, socials, emails, phones, addresses, {sel_status} AS status
                FROM read_parquet(?)
                WHERE bbox.xmin >= ? AND bbox.xmax <= ? AND bbox.ymin >= ? AND bbox.ymax <= ?
                  AND websites IS NOT NULL AND len(websites) > 0
                  AND ({like}) {('AND ' + ' AND '.join(extra)) if extra else ''}
                LIMIT {int(limit)}"""
            xmin, ymin, xmax, ymax = bbox
            rows = con.execute(sql, [source, xmin, xmax, ymin, ymax] + [f"%{k}%" for k in keywords]).fetchall()
        except OvertureError:
            raise
        except Exception as e:  # noqa: BLE001
            raise OvertureError(f"Overture query failed ({type(e).__name__})") from e
        return [self._to_lead(r, city) for r in rows if r[0]]

    def _to_lead(self, row, city: str) -> dict:
        name, _cat, websites, socials, emails, phones, addresses, _status = row
        addr = (addresses or [None])[0] or {}
        street = addr.get("freeform") or ""
        address = ", ".join(x for x in [street, addr.get("postcode") or "", addr.get("locality") or ""] if x)
        cc = (addr.get("country") or "").upper()
        email = ""
        for e in emails or []:
            email = clean_email(e or "")
            if email:
                break
        phone = _first(phones)
        return {
            "business_name": str(name).strip(),
            "website": _first(websites),
            "email": email,
            "phone": normalize_phone(phone, cc) if phone else "",
            "facebook_url": _social(socials, "facebook.com"),
            "instagram_url": _social(socials, "instagram.com"),
            "linkedin_url": _social(socials, "linkedin.com", "/company"),
            "address": address,
            "region": addr.get("locality") or city,
            "country": cc,
            "source": "overture",
        }
