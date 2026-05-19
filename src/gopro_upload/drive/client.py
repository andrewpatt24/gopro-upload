from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

MIGRATOR_VERSION = "1"
UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3/files"


@dataclass
class DriveFileInfo:
    file_id: str
    name: str
    size: int | None
    md5_checksum: str | None
    gopro_media_id: str | None


class DriveClient:
    def __init__(self, creds: Credentials) -> None:
        self._creds = creds
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._http = httpx.Client(timeout=300.0)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DriveClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _auth_header(self) -> dict[str, str]:
        if not self._creds.valid:
            from google.auth.transport.requests import Request

            self._creds.refresh(Request())
        return {"Authorization": f"Bearer {self._creds.token}"}

    def create_folder(self, name: str) -> dict[str, Any]:
        """Create a folder (visible to this app under drive.file scope)."""
        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "appProperties": {
                "created_by": "gopro-upload",
                "migrator_version": MIGRATOR_VERSION,
            },
        }
        return (
            self._service.files()
            .create(body=metadata, fields="id,name,mimeType,webViewLink")
            .execute()
        )

    def test_folder_access(self, folder_id: str) -> dict[str, Any]:
        return (
            self._service.files()
            .get(fileId=folder_id, fields="id,name,mimeType")
            .execute()
        )

    def list_folder_files(self, folder_id: str) -> list[DriveFileInfo]:
        files: list[DriveFileInfo] = []
        page_token: str | None = None
        q = f"'{folder_id}' in parents and trashed=false"
        while True:
            result = (
                self._service.files()
                .list(
                    q=q,
                    spaces="drive",
                    fields="nextPageToken, files(id,name,size,md5Checksum,appProperties)",
                    pageToken=page_token,
                    pageSize=100,
                )
                .execute()
            )
            for f in result.get("files", []):
                props = f.get("appProperties") or {}
                files.append(
                    DriveFileInfo(
                        file_id=f["id"],
                        name=f.get("name", ""),
                        size=int(f["size"]) if f.get("size") else None,
                        md5_checksum=f.get("md5Checksum"),
                        gopro_media_id=props.get("gopro_media_id"),
                    )
                )
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        return files

    def get_file(self, file_id: str) -> DriveFileInfo:
        f = (
            self._service.files()
            .get(
                fileId=file_id,
                fields="id,name,size,md5Checksum,appProperties",
            )
            .execute()
        )
        props = f.get("appProperties") or {}
        return DriveFileInfo(
            file_id=f["id"],
            name=f.get("name", ""),
            size=int(f["size"]) if f.get("size") else None,
            md5_checksum=f.get("md5Checksum"),
            gopro_media_id=props.get("gopro_media_id"),
        )

    def delete_file(self, file_id: str) -> None:
        self._service.files().delete(fileId=file_id).execute()

    def create_resumable_session(
        self,
        *,
        folder_id: str,
        name: str,
        mime_type: str,
        size_bytes: int,
        gopro_media_id: str,
        capture_time: str | None,
    ) -> str:
        metadata = {
            "name": name,
            "parents": [folder_id],
            "mimeType": mime_type,
            "appProperties": {
                "gopro_media_id": gopro_media_id,
                "gopro_capture_time": capture_time or "",
                "migrator_version": MIGRATOR_VERSION,
            },
        }
        response = self._http.post(
            f"{UPLOAD_BASE}?uploadType=resumable",
            headers={
                **self._auth_header(),
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(size_bytes),
            },
            content=json.dumps(metadata),
        )
        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"Failed to start resumable upload: {response.status_code} {response.text}"
            )
        location = response.headers.get("location")
        if not location:
            raise RuntimeError("No resumable session URI in response")
        return location

    def query_upload_offset(self, session_uri: str, total_size: int) -> int:
        """Return number of bytes Drive has received."""
        response = self._http.put(
            session_uri,
            headers={
                **self._auth_header(),
                "Content-Length": "0",
                "Content-Range": f"bytes */{total_size}",
            },
            content=b"",
        )
        if response.status_code == 308:
            received = response.headers.get("range", "")
            if received.startswith("bytes=0-"):
                end = int(received.split("-")[1])
                return end + 1
            return 0
        if response.status_code in (200, 201):
            return total_size
        if response.status_code == 404:
            raise RuntimeError("Resumable session expired (404)")
        raise RuntimeError(
            f"Unexpected status querying upload offset: {response.status_code} {response.text[:200]}"
        )

    def upload_chunk(
        self, session_uri: str, data: bytes, start: int, total_size: int
    ) -> str | None:
        """Upload one chunk. Returns drive file_id when upload completes."""
        end = start + len(data) - 1
        response = self._http.put(
            session_uri,
            headers={
                **self._auth_header(),
                "Content-Length": str(len(data)),
                "Content-Range": f"bytes {start}-{end}/{total_size}",
            },
            content=data,
        )
        if response.status_code in (200, 201):
            body = response.json()
            return body.get("id")
        if response.status_code == 308:
            return None
        raise RuntimeError(
            f"Chunk upload failed: {response.status_code} {response.text[:300]}"
        )

    def finalize_or_get_id(self, session_uri: str, total_size: int) -> str | None:
        """If upload already complete, response may include file id."""
        try:
            offset = self.query_upload_offset(session_uri, total_size)
            if offset >= total_size:
                response = self._http.put(
                    session_uri,
                    headers={
                        **self._auth_header(),
                        "Content-Length": "0",
                        "Content-Range": f"bytes */{total_size}",
                    },
                    content=b"",
                )
                if response.status_code in (200, 201):
                    return response.json().get("id")
        except HttpError:
            pass
        return None
