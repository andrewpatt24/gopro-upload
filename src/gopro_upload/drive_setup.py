from __future__ import annotations

from pathlib import Path

from rich.console import Console

from gopro_upload.config import DEFAULT_CONFIG_PATH, Settings
from gopro_upload.drive.auth import get_drive_credentials
from gopro_upload.drive.client import DriveClient

console = Console()

DEFAULT_FOLDER_NAME = "GoPro Migration"


def update_config_folder_id(config_path: Path, folder_id: str) -> None:
    """Update drive_folder_id in config.yaml, preserving other lines when possible."""
    if not config_path.exists():
        config_path.write_text(f'drive_folder_id: "{folder_id}"\n')
        return

    lines = config_path.read_text().splitlines()
    new_lines: list[str] = []
    found = False
    for line in lines:
        if line.strip().startswith("drive_folder_id:"):
            new_lines.append(
                f'drive_folder_id: "{folder_id}"  # created by gopro-upload (drive.file scope)'
            )
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.insert(0, f'drive_folder_id: "{folder_id}"')
    config_path.write_text("\n".join(new_lines) + "\n")


def run_init_folder(
    settings: Settings,
    *,
    folder_name: str = DEFAULT_FOLDER_NAME,
    config_path: Path = DEFAULT_CONFIG_PATH,
    write_config: bool = True,
) -> str:
    """
    Create a Drive folder via API (required for drive.file scope).
    Returns folder ID and optionally saves it to config.yaml.
    """
    creds = get_drive_credentials(
        Path(settings.google_credentials_path),
        settings.google_token_path_expanded,
        settings.google_scopes,
    )
    with DriveClient(creds) as drive:
        folder = drive.create_folder(folder_name)
        folder_id = folder["id"]

    if write_config:
        update_config_folder_id(config_path, folder_id)

    console.print(f"[green]Created folder[/green]: {folder.get('name')}")
    console.print(f"Folder ID: {folder_id}")
    if link := folder.get("webViewLink"):
        console.print(f"Open in Drive: {link}")
    if write_config:
        console.print(f"Saved to {config_path}")
    console.print(
        "\n[dim]Using drive.file scope — this app can only see files/folders it creates.[/dim]"
    )
    return folder_id
