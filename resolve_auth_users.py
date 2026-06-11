#!/usr/bin/env python3
"""
Reassigns users in the auth database after duplicate entity resolution.

For every entry in `removed_entities` (main DB) where resolved_in_auth_db = false,
this script:
  1. Finds all entity_users rows in the auth DB for the removed entity.
  2. For each user:
       - If already associated with the kept entity  → deletes the redundant row.
       - If not yet associated with the kept entity  → updates entity_id to the kept entity.
  3. Marks resolved_in_auth_db = true in the main DB.

Each removed entity is handled in its own transaction pair (auth DB + main DB),
so a failure on one entry does not block the rest.

Usage:
    python resolve_auth_users.py               # interactive preview + confirm
    python resolve_auth_users.py --dry-run     # show changes, touch nothing
    python resolve_auth_users.py --yes         # non-interactive, apply immediately

Connections are read from .env:
    DSN       — main (flippro) database
    AUTH_DSN  — auth database
Both can be overridden with --dsn / --auth-dsn.
"""

import argparse
import logging
import os
import re
import sys

import psycopg2
from dotenv import load_dotenv
from tabulate import tabulate

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("resolve_auth_users.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _safe_dsn(dsn: str) -> str:
    return re.sub(r"(password\s*=\s*)\S+", r"\1***", dsn, flags=re.IGNORECASE)


def connect(dsn: str, label: str):
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = False
        logger.info(f"Connected to {label}: {_safe_dsn(dsn)}")
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"Cannot connect to {label}: {e}")
        sys.exit(1)


def fetch(cur, sql: str, params=None) -> list[dict]:
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def execute(cur, sql: str, params=None, dry_run: bool = False) -> int:
    if dry_run:
        logger.info(f"[DRY-RUN] {sql.strip()[:120]}  params={params}")
        return 0
    cur.execute(sql, params)
    return cur.rowcount


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def load_pending(main_cur) -> list[dict]:
    """Return all removed_entities rows not yet resolved in the auth DB."""
    return fetch(
        main_cur,
        """
        SELECT removed_entity_id, removed_client_key, entity_name,
               kept_entity_id,    kept_client_key,    removed_at
        FROM   removed_entities
        WHERE  resolved_in_auth_db = false
        ORDER  BY removed_at
        """,
    )


def load_affected_users(auth_cur, removed_entity_id: str) -> list[dict]:
    """Return entity_users rows for the removed entity."""
    return fetch(
        auth_cur,
        "SELECT id, user_id, entity_id FROM entity_users WHERE entity_id = %s",
        (removed_entity_id,),
    )


def already_assigned(auth_cur, user_id: str, kept_entity_id: str) -> bool:
    """True if the user already has an entity_users row for the kept entity."""
    rows = fetch(
        auth_cur,
        "SELECT 1 FROM entity_users WHERE user_id = %s AND entity_id = %s LIMIT 1",
        (user_id, kept_entity_id),
    )
    return bool(rows)


def process_entry(entry: dict, main_cur, auth_cur, dry_run: bool) -> bool:
    """
    Reassign all users of a removed entity to the kept entity.
    Returns True on success.
    """
    removed_id  = entry["removed_entity_id"]
    kept_id     = entry["kept_entity_id"]
    name        = entry["entity_name"]

    users = load_affected_users(auth_cur, removed_id)
    if not users:
        logger.info(f"  '{name}': no entity_users rows — marking resolved")
        execute(
            main_cur,
            "UPDATE removed_entities SET resolved_in_auth_db = true WHERE removed_entity_id = %s",
            (removed_id,),
            dry_run,
        )
        return True

    reassigned = 0
    deleted    = 0

    for row in users:
        user_id = row["user_id"]
        eu_id   = row["id"]

        if already_assigned(auth_cur, user_id, kept_id):
            # User already belongs to the kept entity — remove the redundant row
            execute(
                auth_cur,
                "DELETE FROM entity_users WHERE id = %s",
                (eu_id,),
                dry_run,
            )
            logger.info(f"  user {user_id}: already on kept entity — deleted redundant row {eu_id}")
            deleted += 1
        else:
            execute(
                auth_cur,
                "UPDATE entity_users SET entity_id = %s WHERE id = %s",
                (kept_id, eu_id),
                dry_run,
            )
            logger.info(f"  user {user_id}: reassigned to kept entity {kept_id}")
            reassigned += 1

    execute(
        main_cur,
        "UPDATE removed_entities SET resolved_in_auth_db = true WHERE removed_entity_id = %s",
        (removed_id,),
        dry_run,
    )
    logger.info(
        f"  '{name}': {reassigned} reassigned, {deleted} redundant rows deleted — marked resolved"
    )
    return True


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_entry_summary(entry: dict, users: list[dict], kept_users: set[str]) -> None:
    """Print a preview table for one removed entity."""
    print(f"\n{'─' * 70}")
    print(f"  Removed entity : {entry['entity_name']}")
    print(f"  Removed ID     : {entry['removed_entity_id']}")
    print(f"  Kept entity ID : {entry['kept_entity_id']}")
    print(f"  Removed at     : {entry['removed_at']}")
    print()

    if not users:
        print("  No entity_users rows — nothing to reassign.\n")
        return

    rows = []
    for u in users:
        action = "DELETE redundant" if u["user_id"] in kept_users else "REASSIGN → kept"
        rows.append([u["user_id"], action])

    print(tabulate(rows, headers=["User ID", "Action"], tablefmt="simple"))
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Reassign auth-DB users after entity resolution.")
    p.add_argument("--dsn",      default=os.getenv("DSN"),
                   help="Main DB DSN (overrides DSN in .env)")
    p.add_argument("--auth-dsn", default=os.getenv("AUTH_DSN"),
                   help="Auth DB DSN (overrides AUTH_DSN in .env)")
    p.add_argument("--dry-run",  action="store_true",
                   help="Preview changes without touching either database")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Apply changes without per-entry confirmation prompts")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if not args.dsn:
        print("Error: no main DB DSN. Set DSN= in .env or pass --dsn.", file=sys.stderr)
        sys.exit(1)
    if not args.auth_dsn:
        print("Error: no auth DB DSN. Set AUTH_DSN= in .env or pass --auth-dsn.", file=sys.stderr)
        sys.exit(1)

    main_conn = connect(args.dsn,      "main DB")
    auth_conn = connect(args.auth_dsn, "auth DB")

    main_cur = main_conn.cursor()
    auth_cur = auth_conn.cursor()

    pending = load_pending(main_cur)

    if not pending:
        print("Nothing to do — all removed entities are already resolved in the auth DB.")
        main_conn.close()
        auth_conn.close()
        return

    print(f"\n{len(pending)} removed entit{'y' if len(pending) == 1 else 'ies'} pending auth-DB resolution.\n")

    processed = skipped = 0

    for entry in pending:
        removed_id = entry["removed_entity_id"]
        kept_id    = entry["kept_entity_id"]

        users      = load_affected_users(auth_cur, removed_id)
        kept_users = {
            row["user_id"]
            for row in fetch(
                auth_cur,
                "SELECT user_id FROM entity_users WHERE entity_id = %s",
                (kept_id,),
            )
        }

        print_entry_summary(entry, users, kept_users)

        if not args.yes and not args.dry_run:
            answer = input("Apply? [y/N/q]: ").strip().lower()
            if answer == "q":
                print("Aborted — no further entries processed.")
                break
            if answer != "y":
                print("  Skipped.")
                skipped += 1
                continue

        try:
            ok = process_entry(entry, main_cur, auth_cur, args.dry_run)
            if ok:
                if not args.dry_run:
                    auth_conn.commit()
                    main_conn.commit()
                    logger.info(f"Committed entry: {entry['entity_name']}")
                processed += 1
        except Exception as exc:
            auth_conn.rollback()
            main_conn.rollback()
            logger.error(f"Failed on '{entry['entity_name']}': {exc}")
            skipped += 1

    main_cur.close()
    auth_cur.close()
    main_conn.close()
    auth_conn.close()

    verb = "would be" if args.dry_run else "were"
    print(f"\nDone. {processed} entit{'y' if processed == 1 else 'ies'} {verb} resolved, {skipped} skipped.")


if __name__ == "__main__":
    main()
