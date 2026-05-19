from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from gopro_upload.config import Settings
from gopro_upload.db import Asset, Database
from gopro_upload.drive.auth import get_drive_credentials
from gopro_upload.drive.client import DriveClient, DriveFileInfo
from gopro_upload.gopro.auth import load_gopro_auth
from gopro_upload.gopro.client import GoProClient
from gopro_upload.inventory import run_inventory

console = Console()


def run_verify(settings: Settings, *, refresh_inventory: bool = True) -> Path:
    if refresh_inventory:
        console.print("Refreshing GoPro inventory...")
        run_inventory(settings)

    auth = load_gopro_auth(
        config_token=settings.gopro_access_token,
        config_user_id=settings.gopro_user_id,
    )
    if not auth:
        raise RuntimeError("GoPro credentials missing")
    if not settings.drive_folder_id:
        raise RuntimeError("drive_folder_id not set")

    creds = get_drive_credentials(
        settings.google_credentials_path,
        settings.google_token_path_expanded,
        settings.google_scopes,
    )

    db = Database(settings.db_path_expanded)
    db.init_schema()

    gopro_ids: dict[str, dict] = {}
    with GoProClient(auth) as gopro:
        for item in gopro.iter_media(per_page=settings.per_page):
            gopro_ids[item.media_id] = {
                "filename": item.display_name,
                "size_bytes": item.size_bytes,
            }

    with DriveClient(creds) as drive:
        drive_files = drive.list_folder_files(settings.drive_folder_id)

    drive_by_gopro_id: dict[str, DriveFileInfo] = {}
    drive_orphans: list[DriveFileInfo] = []
    for f in drive_files:
        if f.gopro_media_id:
            drive_by_gopro_id[f.gopro_media_id] = f
        else:
            drive_orphans.append(f)

    with db.session() as conn:
        sqlite_assets = {a.media_id: a for a in db.all_assets(conn)}

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": [],
        "missing_on_drive": [],
        "orphan_on_drive": [],
        "mismatch": [],
        "stale_sqlite": [],
    }

    for media_id, meta in gopro_ids.items():
        sa = sqlite_assets.get(media_id)
        df = drive_by_gopro_id.get(media_id)
        if df and sa and sa.status == "done":
            if _sizes_match(df.size, meta["size_bytes"]):
                report["ok"].append(_entry(media_id, meta, sa, df))
            else:
                report["mismatch"].append(
                    {**_entry(media_id, meta, sa, df), "reason": "size_mismatch"}
                )
        elif not df:
            report["missing_on_drive"].append(
                {
                    "media_id": media_id,
                    "filename": meta["filename"],
                    "sqlite_status": sa.status if sa else None,
                }
            )
        elif df and sa and sa.status != "done":
            report["missing_on_drive"].append(
                {
                    "media_id": media_id,
                    "filename": meta["filename"],
                    "sqlite_status": sa.status,
                    "drive_file_id": df.file_id,
                    "note": "on_drive_but_sqlite_not_done",
                }
            )

    for media_id, sa in sqlite_assets.items():
        if sa.status == "done" and media_id not in gopro_ids:
            report["stale_sqlite"].append(
                {"media_id": media_id, "filename": sa.filename, "reason": "not_in_gopro"}
            )
        if sa.status == "done" and media_id not in drive_by_gopro_id:
            report["stale_sqlite"].append(
                {"media_id": media_id, "filename": sa.filename, "reason": "not_on_drive"}
            )

    for f in drive_orphans:
        report["orphan_on_drive"].append(
            {"file_id": f.file_id, "name": f.name, "size": f.size}
        )
    for media_id, df in drive_by_gopro_id.items():
        if media_id not in gopro_ids:
            report["orphan_on_drive"].append(
                {
                    "file_id": df.file_id,
                    "name": df.name,
                    "gopro_media_id": media_id,
                    "reason": "gopro_id_not_in_cloud",
                }
            )

    settings.reports_dir_expanded.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = settings.reports_dir_expanded / f"verify-{ts}.json"
    path.write_text(json.dumps(report, indent=2))

    _print_report(report)
    console.print(f"\nReport saved: {path}")
    return path


def _sizes_match(drive_size: int | None, gopro_size: int | None) -> bool:
    if drive_size is None or gopro_size is None:
        return True
    return drive_size == gopro_size


def _entry(media_id: str, meta: dict, sa: Asset, df: DriveFileInfo) -> dict:
    return {
        "media_id": media_id,
        "filename": meta["filename"],
        "sqlite_status": sa.status,
        "drive_file_id": df.file_id,
        "size_gopro": meta["size_bytes"],
        "size_drive": df.size,
    }


def _print_report(report: dict) -> None:
    table = Table(title="Verification summary")
    table.add_column("Category", style="cyan")
    table.add_column("Count", justify="right")
    for key in ("ok", "missing_on_drive", "orphan_on_drive", "mismatch", "stale_sqlite"):
        table.add_row(key, str(len(report[key])))
    console.print(table)

    if report["missing_on_drive"]:
        console.print("\n[yellow]Missing on Drive[/yellow] (run migrate):")
        for item in report["missing_on_drive"][:20]:
            console.print(f"  - {item.get('filename')} ({item.get('media_id')})")
        if len(report["missing_on_drive"]) > 20:
            console.print(f"  ... and {len(report['missing_on_drive']) - 20} more")
