from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from gopro_upload import __version__
from gopro_upload.config import DEFAULT_CONFIG_PATH, Settings, load_settings
from gopro_upload.db import Database
from gopro_upload.drive.auth import get_drive_credentials
from gopro_upload.drive.client import DriveClient
from gopro_upload.gopro.auth import GoProAuth, load_gopro_auth, save_gopro_auth
from gopro_upload.gopro.client import GoProClient
from gopro_upload.inventory import run_inventory
from gopro_upload.drive_setup import run_init_folder
from gopro_upload.transfer import run_migrate
from gopro_upload.verify import run_verify

app = typer.Typer(
    name="gopro-upload",
    help="Migrate GoPro Plus cloud media to Google Drive (resumable, low-disk).",
)
auth_app = typer.Typer(help="Configure authentication")
app.add_typer(auth_app, name="auth")

console = Console()


def _settings(config: Path | None) -> Settings:
    return load_settings(config or DEFAULT_CONFIG_PATH)


@auth_app.command("gopro")
def auth_gopro(
    token: Optional[str] = typer.Option(None, help="gp_access_token"),
    user_id: Optional[str] = typer.Option(None, help="gp_user_id"),
) -> None:
    """Save GoPro Plus cookies (from browser DevTools)."""
    if not token:
        token = typer.prompt("gp_access_token", hide_input=True)
    if not user_id:
        user_id = typer.prompt("gp_user_id")
    auth = GoProAuth(access_token=token.strip(), user_id=user_id.strip())
    path = save_gopro_auth(auth)
    with GoProClient(auth) as client:
        client.validate_user()
    console.print(f"[green]GoPro auth OK[/green], saved to {path}")


@auth_app.command("google")
def auth_google(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    force: bool = typer.Option(False, help="Re-run OAuth flow"),
) -> None:
    """Run Google OAuth desktop flow."""
    settings = _settings(config)
    creds_path = Path(settings.google_credentials_path)
    get_drive_credentials(
        creds_path,
        settings.google_token_path_expanded,
        settings.google_scopes,
        force_reauth=force,
    )
    console.print(
        f"[green]Google auth OK[/green], token at {settings.google_token_path_expanded}"
    )


@app.command()
def doctor(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Test GoPro and Google Drive connectivity."""
    settings = _settings(config)
    ok = True

    auth = load_gopro_auth(
        config_token=settings.gopro_access_token,
        config_user_id=settings.gopro_user_id,
    )
    if not auth:
        console.print("[red]GoPro:[/red] no credentials (run auth gopro)")
        ok = False
    else:
        try:
            with GoProClient(auth) as client:
                client.validate_user()
                count = sum(1 for _ in client.iter_media(per_page=1, max_pages=1))
            console.print(
                f"[green]GoPro:[/green] OK (sample page has media: {count > 0})"
            )
        except Exception as e:
            console.print(f"[red]GoPro:[/red] {e}")
            ok = False

    try:
        creds = get_drive_credentials(
            Path(settings.google_credentials_path),
            settings.google_token_path_expanded,
            settings.google_scopes,
        )
        with DriveClient(creds) as drive:
            if settings.drive_folder_id:
                folder = drive.test_folder_access(settings.drive_folder_id)
                console.print(
                    f"[green]Drive:[/green] OK — folder '{folder.get('name')}'"
                )
            else:
                console.print(
                    "[yellow]Drive:[/yellow] auth OK — run: gopro-upload init-folder"
                )
                ok = False
    except Exception as e:
        err = str(e)
        console.print(f"[red]Drive:[/red] {e}")
        if "notFound" in err or "File not found" in err:
            console.print(
                "[yellow]Hint:[/yellow] With drive.file scope you cannot use a folder "
                "created manually in Drive. Run:\n"
                "  gopro-upload init-folder\n"
                "Then: gopro-upload auth google --force  (if you recently changed scopes)"
            )
        ok = False

    if not ok:
        raise typer.Exit(1)
    console.print("[green]All checks passed[/green]")


@app.command("init-folder")
def init_folder(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    name: str = typer.Option("GoPro Migration", help="Folder name in Google Drive"),
    no_write: bool = typer.Option(False, help="Print folder ID only; do not update config.yaml"),
) -> None:
    """Create a Google Drive folder for uploads (required for drive.file scope)."""
    config_path = config or DEFAULT_CONFIG_PATH
    settings = _settings(config)
    run_init_folder(
        settings,
        folder_name=name,
        config_path=config_path,
        write_config=not no_write,
    )


@app.command()
def inventory(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    max_pages: Optional[int] = typer.Option(None, help="Limit pages (testing)"),
    force_refresh: bool = typer.Option(
        False, help="Update metadata even for done items"
    ),
) -> None:
    """Scan GoPro cloud library into SQLite."""
    settings = _settings(config)
    run_inventory(settings, max_pages=max_pages, force_refresh=force_refresh)


@app.command()
def migrate(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    limit: Optional[int] = typer.Option(None, help="Max files per run"),
) -> None:
    """Transfer pending assets GoPro → Google Drive."""
    settings = _settings(config)
    run_migrate(settings, limit=limit)


@app.command()
def status(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Show migration progress from SQLite."""
    settings = _settings(config)
    db = Database(settings.db_path_expanded)
    db.init_schema()
    with db.session() as conn:
        summary = db.status_summary(conn)

    table = Table(title=f"Migration status (v{__version__})")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    table.add_column("Bytes", justify="right")
    for st, info in sorted(summary["by_status"].items()):
        table.add_row(st, str(info["count"]), _fmt_bytes(info["bytes"]))
    console.print(table)
    console.print(f"Total assets: {summary['total']}, done: {summary['done']}")


@app.command()
def verify(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    no_refresh: bool = typer.Option(False, help="Skip GoPro inventory refresh"),
) -> None:
    """Reconcile GoPro, Drive, and SQLite."""
    settings = _settings(config)
    run_verify(settings, refresh_inventory=not no_refresh)


@app.command()
def failures(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    limit: int = typer.Option(20, help="Max rows to show"),
) -> None:
    """Show recent failed transfers and error messages."""
    settings = _settings(config)
    db = Database(settings.db_path_expanded)
    db.init_schema()
    with db.session() as conn:
        rows = conn.execute(
            """
            SELECT filename, status, bytes_uploaded, size_bytes, attempt_count, last_error, updated_at
            FROM assets
            WHERE status IN ('failed', 'transferring')
               OR last_error IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    if not rows:
        console.print("[green]No failures recorded[/green]")
        return
    table = Table(title="Recent failures / errors")
    table.add_column("File")
    table.add_column("Status")
    table.add_column("Progress")
    table.add_column("Error")
    for r in rows:
        prog = ""
        if r["size_bytes"]:
            pct = 100 * (r["bytes_uploaded"] or 0) / r["size_bytes"]
            prog = f"{pct:.0f}% ({r['bytes_uploaded'] or 0:,}/{r['size_bytes']:,})"
        err = (r["last_error"] or "")[:80]
        table.add_row(r["filename"], r["status"], prog, err)
    console.print(table)


@app.command("retry-failed")
def retry_failed(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Reset failed assets to pending."""
    settings = _settings(config)
    db = Database(settings.db_path_expanded)
    db.init_schema()
    with db.session() as conn:
        n = db.reset_failed(conn)
    console.print(f"[green]Reset {n} failed asset(s) to pending[/green]")


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


if __name__ == "__main__":
    app()
