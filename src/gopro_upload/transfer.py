from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

from gopro_upload.config import Settings
from gopro_upload.db import Asset, Database
from gopro_upload.drive.client import DriveClient
from gopro_upload.gopro.auth import load_gopro_auth
from gopro_upload.gopro.client import GoProAPIError, GoProClient
from gopro_upload.gopro.client import DownloadInfo

console = Console()


def run_migrate(settings: Settings, *, limit: int | None = None) -> None:
    auth = load_gopro_auth(
        config_token=settings.gopro_access_token,
        config_user_id=settings.gopro_user_id,
    )
    if not auth:
        raise RuntimeError("GoPro credentials missing. Run: gopro-upload auth gopro")
    if not settings.drive_folder_id:
        raise RuntimeError(
            "drive_folder_id not set. Run: gopro-upload init-folder"
        )

    from gopro_upload.drive.auth import get_drive_credentials

    creds = get_drive_credentials(
        settings.google_credentials_path,
        settings.google_token_path_expanded,
        settings.google_scopes,
    )

    db = Database(settings.db_path_expanded)
    db.init_schema()
    chunk_size = settings.chunk_size_bytes

    with GoProClient(auth) as gopro, DriveClient(creds) as drive, db.session() as conn:
        queue = list(
            db.iter_work_queue(
                conn, stale_minutes=settings.stale_transfer_minutes, limit=limit
            )
        )
        if not queue:
            console.print("[yellow]No pending work[/yellow]")
            return

        console.print(f"Processing {len(queue)} asset(s)...")
        for asset in queue:
            try:
                _transfer_one(
                    settings=settings,
                    db=db,
                    conn=conn,
                    gopro=gopro,
                    drive=drive,
                    asset=asset,
                    chunk_size=chunk_size,
                )
            except GoProAPIError as e:
                if e.status_code == 401:
                    console.print("[red]GoPro token expired. Refresh cookies and retry.[/red]")
                    raise
                _mark_failed(db, conn, asset, str(e))
            except Exception as e:
                mid = asset.media_id
                _mark_failed(db, conn, asset, str(e))
                console.print(f"[red]Failed[/red] {asset.filename}: {e}")
                row = db.get_asset(conn, mid)
                if row and row.last_error:
                    console.print(f"  [dim]{row.last_error}[/dim]")


def _mark_failed(db: Database, conn, asset: Asset, error: str) -> None:
    db.update_asset(
        conn,
        asset.media_id,
        status="failed",
        last_error=error[:2000],
        attempt_count=asset.attempt_count + 1,
    )
    db.log_event(conn, asset.media_id, "failed", error[:500])


def _clear_upload_state(db: Database, conn, asset: Asset, drive: DriveClient | None = None) -> None:
    if drive and asset.drive_file_id:
        try:
            drive.delete_file(asset.drive_file_id)
        except Exception:
            pass
    db.update_asset(
        conn,
        asset.media_id,
        bytes_uploaded=0,
        drive_upload_uri=None,
        drive_file_id=None,
    )


def _resolve_transfer_size(
    gopro: GoProClient,
    download: DownloadInfo,
    asset: Asset,
    *,
    inventory_size: int | None,
    filename: str,
) -> int:
    """Use CDN Content-Length; inventory often overstates size and causes HTTP 416."""
    size = download.size_bytes or inventory_size
    probed = gopro.probe_size(download.url)
    if probed:
        if size and probed != size:
            console.print(
                f"[yellow]Size corrected[/yellow] {filename}: "
                f"inventory {size:,} → CDN {probed:,} bytes"
            )
        size = probed
    if not size:
        raise RuntimeError(f"Cannot determine size for {asset.media_id}")
    return size


def _transfer_one(
    *,
    settings: Settings,
    db: Database,
    conn,
    gopro: GoProClient,
    drive: DriveClient,
    asset: Asset,
    chunk_size: int,
) -> None:
    if asset.attempt_count >= settings.max_retries:
        console.print(f"[yellow]Skipping[/yellow] {asset.filename} (max retries)")
        return

    db.update_asset(conn, asset.media_id, status="transferring")
    db.log_event(conn, asset.media_id, "transfer_start")

    download = _resolve_download(gopro, asset)
    size = _resolve_transfer_size(
        gopro, download, asset, inventory_size=asset.size_bytes, filename=asset.filename
    )
    if asset.size_bytes != size:
        db.update_asset(conn, asset.media_id, size_bytes=size)

    mime = asset.mime_type or "video/mp4"
    name = asset.filename

    session_uri = asset.drive_upload_uri
    bytes_done = asset.bytes_uploaded or 0

    # Wrong inventory size may leave a partial Drive upload at the old declared length.
    if bytes_done > size:
        console.print(
            f"[yellow]Resetting[/yellow] {name}: had {bytes_done:,} bytes uploaded "
            f"but CDN file is {size:,} bytes"
        )
        _clear_upload_state(db, conn, asset, drive)
        session_uri = None
        bytes_done = 0

    if session_uri:
        try:
            bytes_done = drive.query_upload_offset(session_uri, size)
        except RuntimeError:
            _clear_upload_state(db, conn, asset, drive)
            session_uri = None
            bytes_done = 0

    if not session_uri:
        session_uri = drive.create_resumable_session(
            folder_id=settings.drive_folder_id,
            name=name,
            mime_type=mime,
            size_bytes=size,
            gopro_media_id=asset.media_id,
            capture_time=asset.capture_time,
        )
        db.update_asset(
            conn,
            asset.media_id,
            drive_upload_uri=session_uri,
            download_url=download.url,
            download_url_expires_at=_iso(download.expires_at),
        )

    db.update_asset(conn, asset.media_id, bytes_uploaded=bytes_done)

    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(name[:40], total=size, completed=bytes_done)

        while bytes_done < size:
            end = min(bytes_done + chunk_size - 1, size - 1)
            try:
                chunk = gopro.fetch_range(download.url, bytes_done, end)
            except GoProAPIError as e:
                if e.status_code == 416:
                    probed = gopro.probe_size(download.url)
                    if probed and probed < size:
                        console.print(
                            f"[yellow]CDN size {probed:,} < expected {size:,} — restarting upload[/yellow]"
                        )
                        size = probed
                        db.update_asset(conn, asset.media_id, size_bytes=size)
                        _clear_upload_state(db, conn, asset, drive)
                        session_uri = drive.create_resumable_session(
                            folder_id=settings.drive_folder_id,
                            name=name,
                            mime_type=mime,
                            size_bytes=size,
                            gopro_media_id=asset.media_id,
                            capture_time=asset.capture_time,
                        )
                        db.update_asset(
                            conn,
                            asset.media_id,
                            drive_upload_uri=session_uri,
                            bytes_uploaded=0,
                        )
                        bytes_done = 0
                        progress.update(task, total=size, completed=0)
                        continue
                raise
            file_id = drive.upload_chunk(session_uri, chunk, bytes_done, size)
            bytes_done += len(chunk)
            db.update_asset(conn, asset.media_id, bytes_uploaded=bytes_done)
            progress.update(task, completed=bytes_done)
            if file_id:
                db.update_asset(
                    conn,
                    asset.media_id,
                    drive_file_id=file_id,
                    drive_upload_uri=None,
                    status="verifying",
                )
                _verify_and_complete(db, conn, drive, asset.media_id, file_id, size)
                console.print(f"[green]Done[/green] {name}")
                return

    file_id = drive.finalize_or_get_id(session_uri, size)
    if not file_id and bytes_done >= size:
        raise RuntimeError("Upload finished but no file id returned")

    if file_id:
        db.update_asset(
            conn,
            asset.media_id,
            drive_file_id=file_id,
            drive_upload_uri=None,
            status="verifying",
        )
        _verify_and_complete(db, conn, drive, asset.media_id, file_id, size)
        console.print(f"[green]Done[/green] {name}")


def _resolve_download(gopro: GoProClient, asset: Asset) -> DownloadInfo:
    expires = asset.download_url_expires_at
    if asset.download_url and expires:
        try:
            exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if exp_dt > datetime.now(timezone.utc):
                return DownloadInfo(
                    url=asset.download_url,
                    size_bytes=asset.size_bytes,
                    expires_at=exp_dt,
                )
        except ValueError:
            pass
    info = gopro.get_download_info(asset.media_id)
    return info


def _verify_and_complete(
    db: Database, conn, drive: DriveClient, media_id: str, file_id: str, expected_size: int
) -> None:
    info = drive.get_file(file_id)
    if info.size is not None and info.size != expected_size:
        db.update_asset(
            conn,
            media_id,
            status="failed",
            last_error=f"Size mismatch: drive={info.size} expected={expected_size}",
        )
        db.log_event(conn, media_id, "verify_failed", f"size {info.size} != {expected_size}")
        return
    if info.gopro_media_id and info.gopro_media_id != media_id:
        db.update_asset(
            conn,
            media_id,
            status="failed",
            last_error="appProperties gopro_media_id mismatch",
        )
        return
    db.update_asset(conn, media_id, status="done", last_error=None)
    db.log_event(conn, media_id, "done")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
