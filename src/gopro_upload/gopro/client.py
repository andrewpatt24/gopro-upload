from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import httpx

from gopro_upload.gopro.auth import GoProAuth

API_BASE = "https://api.gopro.com"
ACCEPT = "application/vnd.gopro.jk.media+json; version=2.0.0"
SEARCH_FIELDS = "id,created_at,captured_at,content_title,filename,file_extension,file_size,type"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class GoProAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class MediaItem:
    media_id: str
    filename: str
    size_bytes: int | None
    mime_type: str | None
    capture_time: str | None
    file_extension: str | None

    @property
    def display_name(self) -> str:
        ext = (self.file_extension or "").lstrip(".").upper()
        name = self.filename
        if ext and not name.upper().endswith(f".{ext}"):
            return f"{name}.{ext.lower()}"
        return name


@dataclass
class DownloadInfo:
    url: str
    size_bytes: int | None
    expires_at: datetime | None


class GoProClient:
    def __init__(self, auth: GoProAuth, *, timeout: float = 120.0) -> None:
        self.auth = auth
        self._client = httpx.Client(
            base_url=API_BASE,
            timeout=timeout,
            headers={
                "Accept": ACCEPT,
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": USER_AGENT,
            },
            cookies={
                "gp_access_token": auth.access_token,
                "gp_user_id": auth.user_id,
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GoProClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _check(self, response: httpx.Response) -> None:
        if response.status_code == 401:
            raise GoProAPIError(
                "GoPro authentication failed (401). Refresh gp_access_token from "
                "https://plus.gopro.com/media-library/ DevTools cookies.",
                status_code=401,
            )
        if response.status_code >= 400:
            raise GoProAPIError(
                f"GoPro API error {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
            )

    def validate_user(self) -> dict[str, Any]:
        response = self._client.get("/media/user")
        self._check(response)
        return response.json()

    def iter_media(
        self, *, per_page: int = 30, max_pages: int | None = None
    ) -> Iterator[MediaItem]:
        page = 1
        pages_fetched = 0
        while True:
            response = self._client.get(
                "/media/search",
                params={
                    "page": page,
                    "per_page": per_page,
                    "fields": SEARCH_FIELDS,
                },
            )
            self._check(response)
            data = response.json()
            embedded = data.get("_embedded", {})
            for raw in embedded.get("media", []):
                yield self._parse_media(raw)
            pages = data.get("_pages", {})
            total_pages = pages.get("total_pages", page)
            pages_fetched += 1
            if page >= total_pages:
                break
            if max_pages and pages_fetched >= max_pages:
                break
            page += 1

    def _parse_media(self, raw: dict[str, Any]) -> MediaItem:
        ext = raw.get("file_extension") or ""
        mime = _mime_for_extension(ext)
        capture = raw.get("captured_at") or raw.get("created_at")
        size = raw.get("file_size")
        return MediaItem(
            media_id=raw["id"],
            filename=raw.get("filename") or raw.get("content_title") or raw["id"],
            size_bytes=int(size) if size is not None else None,
            mime_type=mime,
            capture_time=capture,
            file_extension=ext,
        )

    def get_download_info(self, media_id: str) -> DownloadInfo:
        response = self._client.get(f"/media/{media_id}/download")
        self._check(response)
        data = response.json()
        url, size = _best_download_url(data)
        if not url:
            raise GoProAPIError(f"No download URL for media {media_id}")
        # Signed URLs typically expire in ~1 hour; refresh conservatively.
        expires = datetime.now(timezone.utc) + timedelta(minutes=50)
        return DownloadInfo(url=url, size_bytes=size, expires_at=expires)

    def probe_size(self, url: str) -> int | None:
        """HEAD or Range probe to confirm remote size."""
        try:
            head = self._client.head(url, follow_redirects=True)
            if head.status_code == 200 and head.headers.get("content-length"):
                return int(head.headers["content-length"])
        except httpx.HTTPError:
            pass
        try:
            response = self._client.get(
                url,
                headers={"Range": "bytes=0-0"},
                follow_redirects=True,
            )
            if response.status_code in (200, 206):
                cr = response.headers.get("content-range", "")
                if sz := _size_from_content_range(cr):
                    return sz
                if response.headers.get("content-length"):
                    return int(response.headers["content-length"])
            if response.status_code == 416:
                cr = response.headers.get("content-range", "")
                if sz := _size_from_content_range(cr):
                    return sz
        except httpx.HTTPError:
            pass
        return None

    def fetch_range(self, url: str, start: int, end: int) -> bytes:
        response = self._client.get(
            url,
            headers={"Range": f"bytes={start}-{end}"},
            follow_redirects=True,
        )
        if response.status_code == 416:
            actual = _size_from_content_range(response.headers.get("content-range", ""))
            raise GoProAPIError(
                f"Range download failed: HTTP 416 (requested bytes {start}-{end}"
                + (f", source size {actual}" if actual else "")
                + ")",
                status_code=416,
            )
        if response.status_code not in (200, 206):
            raise GoProAPIError(
                f"Range download failed: HTTP {response.status_code}",
                status_code=response.status_code,
            )
        return response.content


def _mime_for_extension(ext: str) -> str:
    e = ext.lower().lstrip(".")
    return {
        "mp4": "video/mp4",
        "mov": "video/quicktime",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
    }.get(e, "application/octet-stream")


def _size_from_content_range(content_range: str) -> int | None:
    if "/" in content_range:
        total = content_range.split("/")[-1].strip()
        if total.isdigit():
            return int(total)
    return None


def _best_download_url(data: dict[str, Any]) -> tuple[str | None, int | None]:
    """Pick largest available file from download metadata."""
    embedded = data.get("_embedded", {})
    candidates: list[tuple[int, str, int | None]] = []

    for f in embedded.get("files", []):
        if not f.get("available", True):
            continue
        url = f.get("url")
        if not url:
            continue
        w = int(f.get("width") or 0)
        sz = f.get("file_size") or f.get("size")
        candidates.append((w, url, int(sz) if sz is not None else None))

    for v in embedded.get("variations", []):
        if not v.get("available", True):
            continue
        url = v.get("url")
        if not url:
            continue
        w = int(v.get("width") or 0)
        sz = v.get("file_size") or v.get("size")
        candidates.append((w, url, int(sz) if sz is not None else None))

    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0], reverse=True)
    url = candidates[0][1]
    size = candidates[0][2] or data.get("file_size")
    if size is not None:
        size = int(size)
    return url, size
