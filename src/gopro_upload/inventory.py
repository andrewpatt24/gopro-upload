from __future__ import annotations

from gopro_upload.config import Settings
from gopro_upload.db import Database
from gopro_upload.gopro.client import GoProClient
from gopro_upload.gopro.auth import load_gopro_auth
from rich.console import Console

console = Console()


def run_inventory(settings: Settings, *, max_pages: int | None = None, force_refresh: bool = False) -> dict[str, int]:
    auth = load_gopro_auth(
        config_token=settings.gopro_access_token,
        config_user_id=settings.gopro_user_id,
    )
    if not auth:
        raise RuntimeError(
            "GoPro credentials missing. Run: gopro-upload auth gopro"
        )

    db = Database(settings.db_path_expanded)
    db.init_schema()

    seen = 0
    new = 0

    with GoProClient(auth) as gopro, db.session() as conn:
        run_id = db.start_sync_run(conn, "inventory")
        for item in gopro.iter_media(per_page=settings.per_page, max_pages=max_pages):
            seen += 1
            inserted = db.upsert_asset(
                conn,
                media_id=item.media_id,
                filename=item.display_name,
                size_bytes=item.size_bytes,
                mime_type=item.mime_type,
                capture_time=item.capture_time,
                preserve_done=not force_refresh,
            )
            if inserted:
                new += 1
        db.finish_sync_run(conn, run_id, items_seen=seen, items_new=new)

    console.print(f"[green]Inventory complete[/green]: {seen} items seen, {new} new")
    return {"seen": seen, "new": new}
