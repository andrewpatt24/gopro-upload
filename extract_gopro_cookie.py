#!/usr/bin/env python3
"""Extract GoPro auth values from a browser Cookie header string."""

from __future__ import annotations

import sys


def shell_export(name: str, value: str) -> str:
    """Format value safely for bash: export NAME='...'"""
    escaped = value.replace("'", "'\"'\"'")
    return f"export {name}='{escaped}'"


def parse_cookie_string(cookie_str: str) -> dict[str, str]:
    """Parse semicolon-separated cookie header into name -> value."""
    result: dict[str, str] = {}
    for part in cookie_str.strip().split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        result[name.strip()] = value.strip()
    return result


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: python extract_gopro_cookie.py '<cookie string>'\n"
            "   or: python extract_gopro_cookie.py @cookies.txt",
            file=sys.stderr,
        )
        sys.exit(1)

    arg = sys.argv[1]
    if arg.startswith("@"):
        cookie_str = open(arg[1:], encoding="utf-8").read()
    else:
        cookie_str = " ".join(sys.argv[1:])

    cookies = parse_cookie_string(cookie_str)
    needed = ("gp_access_token", "gp_user_id")
    missing = [k for k in needed if k not in cookies]

    if missing:
        print("Could not find:", ", ".join(missing), file=sys.stderr)
        print("\nFound cookie names:", ", ".join(sorted(cookies)), file=sys.stderr)
        sys.exit(1)

    token = cookies["gp_access_token"]
    user_id = cookies["gp_user_id"]

    print(shell_export("GOPRO_USER_ID", user_id))
    print(shell_export("GOPRO_ACCESS_TOKEN", token))


if __name__ == "__main__":
    main()
