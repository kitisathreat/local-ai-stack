"""CLI that creates the first admin account interactively.

Invoked by `LocalAIStack.ps1 -Setup` after the backend venv is in place
and the SQLite DB has been initialised:

    python -m backend.seed_admin --if-no-admins

With `--if-no-admins`, exits cleanly if any admin user already exists.
Without the flag, always prompts (useful for resetting after deleting
a misconfigured first admin).
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from . import db, passwords


async def _run(if_no_admins: bool) -> int:
    await db.init_db()
    if if_no_admins:
        count = await db.count_admins()
        if count > 0:
            print(f"Admin already configured ({count} admin user(s) exist). No changes.")
            return 0

    print("Create the first admin user.")
    username = input("  Username: ").strip()
    if not username:
        print("Aborted — username required.")
        return 1
    email = input("  Email:    ").strip()
    if not email or "@" not in email:
        print("Aborted — valid email required.")
        return 1
    while True:
        pw1 = getpass.getpass("  Password: ")
        if len(pw1) < 8:
            print("  Password must be at least 8 characters.")
            continue
        pw2 = getpass.getpass("  Confirm:  ")
        if pw1 != pw2:
            print("  Passwords did not match, try again.")
            continue
        break

    try:
        user = await db.create_user(
            username=username,
            email=email,
            password_hash=passwords.hash_password(pw1),
            is_admin=True,
        )
    except Exception as exc:
        print(f"Failed to create user: {exc}")
        return 2
    print(f"Created admin {user['username']} (id {user['id']}).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="backend.seed_admin")
    p.add_argument(
        "--if-no-admins",
        action="store_true",
        help="Skip silently when an admin user already exists.",
    )
    args = p.parse_args()
    return asyncio.run(_run(args.if_no_admins))


if __name__ == "__main__":
    sys.exit(main())
