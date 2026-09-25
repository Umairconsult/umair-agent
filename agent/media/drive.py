"""Uploads generated media files to Google Drive. Two ways to log in (pick ONE):

A) A Google service account (GOOGLE_SERVICE_ACCOUNT_JSON = the downloaded JSON key, squeezed onto
   one base64 line, + GOOGLE_DRIVE_FOLDER_ID = a folder shared with that account as Editor).
   IMPORTANT: Google gives service accounts NO storage space of their own, so this only works
   when GOOGLE_DRIVE_FOLDER_ID is a folder inside a Google Workspace *Shared Drive*. In an
   ordinary "My Drive" folder every upload fails with "storageQuotaExceeded".

B) Your own Google account (GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET +
   GOOGLE_OAUTH_REFRESH_TOKEN - get the refresh token once by running get_drive_token.py on
   your computer). Files are then owned by you and use your normal Drive storage, so this works
   with any ordinary Gmail / My Drive folder.
"""
from __future__ import annotations
import base64
import binascii
import json
import re
import time

import requests

from ..logutil import safe_exc

# Full "drive" scope on purpose: the narrower "drive.file" scope can only see files this app created
# itself, so it cannot see (or upload into) a folder you created and shared with the agent.
SCOPES = ["https://www.googleapis.com/auth/drive"]
UPLOAD_URL = ("https://www.googleapis.com/upload/drive/v3/files"
              "?uploadType=multipart&supportsAllDrives=true&fields=id,webViewLink")
TOKEN_URI = "https://oauth2.googleapis.com/token"
BOUNDARY = "umairagentuploadboundary"


class DriveError(Exception):
    pass


def _decode_service_account_json(raw: str) -> dict:
    """Accepts either the JSON key pasted as-is, or (the normal case) that same JSON
    base64-encoded onto one line."""
    raw = (raw or "").strip()
    if not raw:
        raise DriveError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    try:
        return json.loads(raw)
    except ValueError:
        pass
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
    except (binascii.Error, ValueError) as e:
        raise DriveError("GOOGLE_SERVICE_ACCOUNT_JSON is neither valid JSON nor valid base64") from e
    try:
        return json.loads(decoded)
    except ValueError as e:
        raise DriveError("GOOGLE_SERVICE_ACCOUNT_JSON decoded but is not valid JSON") from e


class Drive:
    def __init__(self, service_account_json: str = "", folder_id: str = "", oauth: dict | None = None):
        if not folder_id:
            raise DriveError("GOOGLE_DRIVE_FOLDER_ID is not set")
        self.folder_id = folder_id
        self.mode = "oauth" if oauth and oauth.get("refresh_token") else "service_account"
        try:
            if self.mode == "oauth":
                if not (oauth.get("client_id") and oauth.get("client_secret")):
                    raise DriveError("GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET are needed together with GOOGLE_OAUTH_REFRESH_TOKEN")
                from google.oauth2.credentials import Credentials
                self._creds = Credentials(token=None, refresh_token=oauth["refresh_token"], token_uri=TOKEN_URI,
                                          client_id=oauth["client_id"], client_secret=oauth["client_secret"], scopes=SCOPES)
                return
            info = _decode_service_account_json(service_account_json)
            from google.oauth2 import service_account
            self._creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        except DriveError:
            raise
        except ImportError as e:
            raise DriveError("the 'google-auth' package isn't installed (see requirements.txt)") from e
        except Exception as e:  # noqa: BLE001 - a malformed key must produce a plain-English error
            raise DriveError(f"the Google login details could not be used as credentials ({type(e).__name__})") from e

    def _token(self) -> str:
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import Request
        try:
            if not self._creds.valid:
                self._creds.refresh(Request())
        except GoogleAuthError as e:
            raise DriveError(f"could not authenticate to Google Drive ({safe_exc(e, 150)})") from e
        return self._creds.token

    def _explain(self, r) -> str:
        """Turns a Google error reply into plain English (never includes any key)."""
        reason, message = "", ""
        try:
            err = (r.json() or {}).get("error") or {}
            message = str(err.get("message", ""))
            reason = str((err.get("errors") or [{}])[0].get("reason", ""))
        except (ValueError, AttributeError, IndexError):
            message = (r.text or "")[:150]
        low = (reason + " " + message).lower()
        if "storagequota" in low or "do not have storage quota" in low or "storage quota" in low:
            if self.mode == "service_account":
                return ("Google says a service account has no storage space of its own, so it cannot save files into an "
                        "ordinary 'My Drive' folder. Fix (pick one): (1) put the folder inside a Google Workspace Shared "
                        "Drive, or (2) log in as yourself instead - run get_drive_token.py once on your computer and add "
                        "GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REFRESH_TOKEN to settings.env.")
            return "Your Google Drive storage is full - free up some space or use another folder."
        if r.status_code == 404:
            return ("Google Drive folder not found - check GOOGLE_DRIVE_FOLDER_ID is the folder's ID (the code at the end "
                    "of its URL), and that the login used can see it (share the folder with the service account's "
                    "client_email as Editor)")
        if r.status_code in (401, 403):
            who = "the service account email (client_email in the JSON key)" if self.mode == "service_account" else "your Google account"
            return (f"Google Drive said 'forbidden' - make sure {who} can edit the destination folder"
                    + (f" ({message[:100]})" if message else ""))
        return f"Google Drive HTTP {r.status_code}: {message[:150]}"

    def upload(self, data: bytes, filename: str, mime_type: str = "image/png") -> dict:
        """Uploads bytes as a new file in the configured folder. Returns {'id': ..., 'link': ...}."""
        metadata = {"name": filename, "parents": [self.folder_id]}
        body = (
            f"--{BOUNDARY}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{BOUNDARY}\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + data + f"\r\n--{BOUNDARY}--".encode("utf-8")
        last = "unknown"
        for attempt in range(3):
            headers = {
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": f"multipart/related; boundary={BOUNDARY}",
            }
            try:
                r = requests.post(UPLOAD_URL, headers=headers, data=body, timeout=120)
            except requests.RequestException as e:
                last = type(e).__name__
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                time.sleep(4 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise DriveError(self._explain(r))
            try:
                out = r.json()
            except ValueError as e:
                raise DriveError("Google Drive sent a non-JSON reply") from e
            if not out.get("id"):
                raise DriveError("Google Drive accepted the upload but returned no file id")
            link = out.get("webViewLink") or f"https://drive.google.com/file/d/{out['id']}/view"
            return {"id": out["id"], "link": link}
        raise DriveError(f"Google Drive unreachable after 3 tries ({last})")

    def ping(self) -> str:
        """Confirms the login works and can see the destination folder. Used by the self-test.
        Returns the folder's name."""
        try:
            r = requests.get(
                f"https://www.googleapis.com/drive/v3/files/{self.folder_id}",
                headers={"Authorization": f"Bearer {self._token()}"},
                params={"fields": "id,name,mimeType,capabilities/canAddChildren", "supportsAllDrives": "true"}, timeout=30)
        except requests.RequestException as e:
            raise DriveError(f"Google Drive unreachable ({type(e).__name__})") from e
        if r.status_code != 200:
            raise DriveError(self._explain(r))
        info = r.json()
        if info.get("mimeType") and info["mimeType"] != "application/vnd.google-apps.folder":
            raise DriveError("GOOGLE_DRIVE_FOLDER_ID points at a file, not a folder")
        if (info.get("capabilities") or {}).get("canAddChildren") is False:
            raise DriveError("this login can see the folder but only as a viewer - share it as Editor")
        return info.get("name", "")


def clean_folder_id(raw: str) -> str:
    """Accepts a bare folder ID or a pasted folder URL and returns just the ID."""
    raw = (raw or "").strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", raw)
    if m:
        return m.group(1)
    return raw.split("?")[0].strip("/")


def drive_configured(cfg) -> bool:
    """True when enough Drive settings exist to try (either login method + a folder)."""
    has_oauth = bool(cfg.google_oauth_refresh_token and cfg.google_oauth_client_id and cfg.google_oauth_client_secret)
    return bool(cfg.google_drive_folder_id and (has_oauth or cfg.google_service_account_json))


def build_drive(cfg) -> "Drive":
    oauth = None
    if cfg.google_oauth_refresh_token:
        oauth = {"client_id": cfg.google_oauth_client_id, "client_secret": cfg.google_oauth_client_secret,
                 "refresh_token": cfg.google_oauth_refresh_token}
    return Drive(cfg.google_service_account_json, clean_folder_id(cfg.google_drive_folder_id), oauth)
