"""CLI that creates or checks the first admin account.

Invoked by the setup wizard and by `LocalAIStack.ps1 -Setup`:

    # Non-interactive (wizard supplies credentials)
    python -m backend.seed_admin --email admin@example.com --password secret --admin

    # Interactive prompt (developer use)
    python -m backend.seed_admin --if-no-admins

    # Check-only (health probe)
    python -m backend.seed_admin --check-only
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from . import db, passwords


async def _run(
    *,
    if_no_admins: bool,
    check_only: bool,
    email: str | None,
    password: str | None,
    username: str | None,
    admin: bool,
) -> int:
    await db.init_db()

    if check_only:
        count = await db.count_admins()
        if count > 0:
            print(f"OK: {count} admin user(s) exist.")
            return 0
        print("FAIL: no admin users found.")
        return 1

    if if_no_admins:
        count = await db.count_admins()
        if count > 0:
            print(f"Admin already configured ({count} admin user(s) exist). No changes.")
            return 0

    # Non-interactive path (wizard/CI supplies all fields)
    if email and password:
        uname = username or email.split("@")[0]
        if len(password) < 8:
            print("Error: password must be at least 8 characters.")
            return 1
        try:
            user = await db.create_user(
                username=uname,
                email=email,
                password_hash=passwords.hash_password(password),
                is_admin=admin,
            )
        except Exception as exc:
            print(f"Failed to create user: {exc}")
            return 2
        print(f"Created {'admin ' if admin else ''}user {user['username']} (id {user['id']}).")
        return 0

    # Interactive path
    print("Create the first admin user.")
    uname = (username or input("  Username: ")).strip()
    if not uname:
        print("Aborted — username required.")
        return 1
    if not email:
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
        password = pw1
        break

    try:
        user = await db.create_user(
            username=uname,
            email=email,
            password_hash=passwords.hash_password(password),
            is_admin=True,
        )
    except Exception as exc:
        print(f"Failed to create user: {exc}")
        return 2
    print(f"Created admin {user['username']} (id {user['id']}).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="backend.seed_admin")
    p.add_argument("--if-no-admins", action="store_true",
                   help="Skip silently when an admin user already exists.")
    p.add_argument("--check-only", action="store_true",
                   help="Exit 0 if ≥1 admin exists, exit 1 if none (no mutations).")
    p.add_argument("--email", help="Admin e-mail (non-interactive mode).")
    p.add_argument("--password", help="Admin password (non-interactive mode).")
    p.add_argument("--username", help="Username (defaults to email local part).")
    p.add_argument("--admin", action="store_true", default=True,
                   help="Mark the created user as admin (default: true).")
    args = p.parse_args()
    return asyncio.run(_run(
        if_no_admins=args.if_no_admins,
        check_only=args.check_only,
        email=args.email,
        password=args.password,
        username=args.username,
        admin=args.admin,
    ))


if __name__ == "__main__":
    sys.exit(main())
