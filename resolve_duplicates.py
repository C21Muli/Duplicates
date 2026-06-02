#!/usr/bin/env python3
"""
Interactive / batch script for resolving duplicate entities in test_flippro_hostke.

For each duplicate group it:
  1. Migrates appointments, insurance mappings, branches, and other FK refs
     from entity/entities to be removed → entity being kept.
  2. Logs removed entities to the `removed_entities` table (for auth-DB cleanup).
  3. Deletes the removed entity (CASCADE cleans up entity_entity_types).

Modes
-----
Interactive (default):
    python resolve_duplicates.py

Batch (reads decisions from CSV):
    python resolve_duplicates.py --batch decisions.csv

Other flags:
    --dry-run          Show what would happen without touching the DB.
    --group N          Process only group N (1-based, same numbering as analysis output).
    --threshold N      Fuzzy similarity threshold (default: 85).
    --dsn "..."        PostgreSQL DSN (default: dbname=test_flippro_hostke).

Batch CSV format (header required):
    group_number,action,keep_entity_id
    1,ACCEPT,
    2,OVERRIDE,ea736d14-fd42-4dbb-97e0-f871b2094f96
    3,SKIP,
    4,NOT_DUPLICATE,

Actions: ACCEPT | OVERRIDE | SKIP | NOT_DUPLICATE
"""

import argparse
import csv
import logging
import re
import sys
from collections import defaultdict
from itertools import combinations
import psycopg2
from rapidfuzz import fuzz
from tabulate import tabulate
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("resolve_duplicates.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fuzzy scoring (mirrors analysis script)
# ---------------------------------------------------------------------------

_AMPERSAND_RE = re.compile(r"\s*&\s*")
_PUNCT_RE     = re.compile(r"[^\w\s]")
_SPACE_RE     = re.compile(r"\s+")
_SUFFIX_RE    = re.compile(
    r"\s*\b(ltd|limited|llp|llc|co|company|inc|plc|pvt|pty|group|associates?)\b\.?\s*$",
    re.IGNORECASE,
)


def _normalize(s: str) -> str:
    s = _AMPERSAND_RE.sub(" and ", s)
    s = _PUNCT_RE.sub(" ", s)
    return _SPACE_RE.sub(" ", s).strip().lower()


def _strip_suffix(s: str) -> str:
    return _SUFFIX_RE.sub("", s).strip()


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    an, bn   = _normalize(a),     _normalize(b)
    as_, bs_ = _strip_suffix(an), _strip_suffix(bn)
    return max(
        fuzz.ratio(an, bn),
        fuzz.token_sort_ratio(an, bn),
        fuzz.ratio(as_, bs_),
        fuzz.token_sort_ratio(as_, bs_),
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def connect(dsn: str):
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = False
        logger.info(f"Connected to: {dsn}")
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"Cannot connect: {e}")
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
# Data loading
# ---------------------------------------------------------------------------

def load_entities(cur) -> list[dict]:
    return fetch(cur, """
        SELECT e.id, e.name, e.email, e.phone_number, e.status,
               e.client_key, e.is_onboarded, e.v_one_id, e.physical_address,
               et.name AS entity_type
        FROM entities e
        LEFT JOIN entity_type et ON et.id = e.entity_type_id
        WHERE e.name IS NOT NULL AND e.name != ''
        ORDER BY e.name
    """)


def load_appointments(cur) -> dict[str, dict]:
    rows = fetch(cur, """
        SELECT service_provider_id::text AS eid,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE task_status = 'COMPLETED') AS completed,
               COUNT(*) FILTER (WHERE task_status = 'PENDING')   AS pending,
               COUNT(*) FILTER (WHERE task_status NOT IN ('COMPLETED','PENDING')) AS other
        FROM appointments
        WHERE service_provider_id IS NOT NULL
        GROUP BY service_provider_id
    """)
    return {r["eid"]: r for r in rows}


def load_insurance_mappings(cur) -> dict[str, list[dict]]:
    rows = fetch(cur, """
        SELECT ete.service_provider_id::text AS sp_id,
               ete.id::text AS mapping_id, ete.status AS mapping_status,
               ins.name AS insurance_name, ins.id::text AS insurance_id,
               ete.can_book_valuation, ete.markup, ete.vatable
        FROM entity_to_entity ete
        JOIN entities ins ON ins.id = ete.insurance_id
        ORDER BY ins.name
    """)
    result = defaultdict(list)
    for r in rows:
        result[r["sp_id"]].append(r)
    return result


def load_branches(cur) -> dict[str, list[dict]]:
    rows = fetch(cur, """
        SELECT eb.entity_id::text AS eid, eb.id::text AS branch_id,
               eb.email, eb.phone_number, eb.physical_address, eb.status,
               aa.name AS admin_area
        FROM entity_branches eb
        LEFT JOIN admin_areas aa ON aa.id = eb.administrative_area_id
        ORDER BY eb.status
    """)
    result = defaultdict(list)
    for r in rows:
        result[r["eid"]].append(r)
    return result


# ---------------------------------------------------------------------------
# Duplicate grouping
# ---------------------------------------------------------------------------

def find_duplicate_groups(entities: list[dict], threshold: int) -> list[list[dict]]:
    n = len(entities)
    adjacency = defaultdict(set)
    total = n * (n - 1) // 2

    with tqdm(total=total, desc="Scanning for duplicates", unit="pair") as pbar:
        for i, j in combinations(range(n), 2):
            if similarity(entities[i]["name"], entities[j]["name"]) >= threshold:
                adjacency[i].add(j)
                adjacency[j].add(i)
            pbar.update(1)

    visited, groups = set(), []
    for start in range(n):
        if start in visited or start not in adjacency:
            continue
        group_indices, queue = set(), [start]
        while queue:
            node = queue.pop()
            if node in visited:
                continue
            visited.add(node)
            group_indices.add(node)
            queue.extend(adjacency[node] - visited)
        if len(group_indices) > 1:
            groups.append([entities[i] for i in sorted(group_indices)])

    return groups


def rank_group(group, appt_map, ins_map, branch_map) -> list[dict]:
    ranked = []
    for e in group:
        a = appt_map.get(e["id"], {})
        ranked.append({
            "entity": e,
            "appointments":      a.get("total", 0),
            "insurance_mappings": len(ins_map.get(e["id"], [])),
            "branches":          len(branch_map.get(e["id"], [])),
        })
    ranked.sort(key=lambda x: (-x["appointments"], -x["insurance_mappings"], -x["branches"]))
    return ranked


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

DIVIDER = "=" * 74


def print_group_summary(g_idx: int, group: list[dict], ranked: list[dict], threshold: int):
    print(f"\n{'#' * 74}")
    print(f"  GROUP {g_idx}:  {group[0]['name']}")
    print(f"{'#' * 74}")

    # Similarity scores
    pairs = [
        [a["name"], b["name"], f"{round(similarity(a['name'], b['name']))}%"]
        for a, b in combinations(group, 2)
        if similarity(a["name"], b["name"]) >= threshold
    ]
    if pairs:
        print("\nSimilarity (pairs meeting threshold):")
        print(tabulate(pairs, headers=["Entity A", "Entity B", "Score"],
                       tablefmt="rounded_outline"))

    # Consolidated summary
    rows = []
    for i, r in enumerate(ranked):
        e = r["entity"]
        rows.append([
            i + 1,
            e["name"],
            e["id"],
            e["client_key"] or "—",
            e["status"],
            r["appointments"],
            r["insurance_mappings"],
            r["branches"],
            "KEEP ✓" if i == 0 else "REMOVE",
        ])
    print()
    print(tabulate(rows, tablefmt="rounded_outline", headers=[
        "#", "Name", "ID", "Client Key", "Status",
        "Appts", "Ins. Maps", "Branches", "Suggestion",
    ]))


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def ensure_removed_entities_table(cur, dry_run: bool):
    execute(cur, """
        CREATE TABLE IF NOT EXISTS removed_entities (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            removed_entity_id   uuid NOT NULL,
            removed_client_key  varchar(50),
            entity_name         varchar(255),
            entity_type         varchar(255),
            kept_entity_id      uuid NOT NULL,
            kept_client_key     varchar(50),
            reason              text,
            removed_at          timestamptz NOT NULL DEFAULT now(),
            resolved_in_auth_db boolean NOT NULL DEFAULT false,
            notes               text
        )
    """, dry_run=dry_run)


def migrate_entity(cur, remove_e: dict, keep_e: dict, dry_run: bool) -> dict:
    """
    Migrate all FK references from remove_e → keep_e, then delete remove_e.
    Returns a summary dict of what was moved / skipped.
    """
    rid, kid = remove_e["id"], keep_e["id"]
    summary  = {}

    # 1. Appointments (service provider side)
    n = execute(cur,
        "UPDATE appointments SET service_provider_id = %s WHERE service_provider_id = %s",
        (kid, rid), dry_run)
    summary["appointments_migrated"] = n
    logger.info(f"  Appointments migrated: {n}")

    # 2. entity_to_entity — service_provider_id
    #    Skip any mapping whose insurance is already mapped to the kept entity.
    mappings_to_move = fetch(cur, """
        SELECT id::text, insurance_id::text
        FROM entity_to_entity
        WHERE service_provider_id = %s
    """, (rid,))

    existing_insurers = {
        r["insurance_id"]
        for r in fetch(cur,
            "SELECT insurance_id::text FROM entity_to_entity WHERE service_provider_id = %s",
            (kid,))
    }

    migrated_maps, skipped_maps = 0, 0
    for m in mappings_to_move:
        if m["insurance_id"] in existing_insurers:
            logger.info(f"  Mapping {m['id']} skipped — kept entity already mapped to this insurer")
            skipped_maps += 1
        else:
            execute(cur,
                "UPDATE entity_to_entity SET service_provider_id = %s WHERE id = %s",
                (kid, m["id"]), dry_run)
            existing_insurers.add(m["insurance_id"])
            migrated_maps += 1

    summary["insurance_mappings_migrated"] = migrated_maps
    summary["insurance_mappings_skipped"]  = skipped_maps
    logger.info(f"  Insurance mappings migrated: {migrated_maps}, skipped (conflict): {skipped_maps}")

    # 3. entity_to_entity — insurance_id (when removed entity IS an insurer)
    n = execute(cur,
        "UPDATE entity_to_entity SET insurance_id = %s WHERE insurance_id = %s",
        (kid, rid), dry_run)
    summary["insurer_role_migrated"] = n
    if n:
        logger.info(f"  entity_to_entity (insurance_id) migrated: {n}")

    # 4. entity_branches — reassign to kept entity
    n = execute(cur,
        "UPDATE entity_branches SET entity_id = %s WHERE entity_id = %s",
        (kid, rid), dry_run)
    summary["branches_migrated"] = n
    logger.info(f"  Branches migrated: {n}")

    # 5. entity_lobs — reassign (skip conflicts on unique constraints)
    lobs_to_move = fetch(cur,
        "SELECT id::text FROM entity_lobs WHERE entity_id = %s", (rid,))
    migrated_lobs = 0
    for lob in lobs_to_move:
        try:
            execute(cur,
                "UPDATE entity_lobs SET entity_id = %s WHERE id = %s",
                (kid, lob["id"]), dry_run)
            migrated_lobs += 1
        except psycopg2.IntegrityError:
            cur.connection.rollback()
            logger.info(f"  entity_lob {lob['id']} skipped — conflict on kept entity")
    summary["lobs_migrated"] = migrated_lobs
    logger.info(f"  Entity LOBs migrated: {migrated_lobs}")

    # 6. dashboard_cards
    n = execute(cur,
        "UPDATE dashboard_cards SET entity_id = %s WHERE entity_id = %s",
        (kid, rid), dry_run)
    summary["dashboard_cards_migrated"] = n
    if n:
        logger.info(f"  Dashboard cards migrated: {n}")

    # 7. Child entities (parent_entity_id)
    n = execute(cur,
        "UPDATE entities SET parent_entity_id = %s WHERE parent_entity_id = %s",
        (kid, rid), dry_run)
    summary["child_entities_migrated"] = n
    if n:
        logger.info(f"  Child entities re-parented: {n}")

    # 8. Log to removed_entities
    execute(cur, """
        INSERT INTO removed_entities (
            removed_entity_id, removed_client_key, entity_name, entity_type,
            kept_entity_id, kept_client_key, reason
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
    """, (
        rid, remove_e["client_key"], remove_e["name"], remove_e["entity_type"],
        kid, keep_e["client_key"],
        f"Resolved as duplicate of {keep_e['name']} (client_key={keep_e['client_key']})"
    ), dry_run)

    # 9. Delete
    n = execute(cur,
        "DELETE FROM entities WHERE id = %s", (rid,), dry_run)
    summary["deleted"] = n
    logger.info(f"  Entity deleted: {n}")

    return summary


def resolve_group(
    conn, cur,
    group: list[dict],
    ranked: list[dict],
    keep_entity: dict,
    dry_run: bool,
) -> bool:
    """Run full migration for one group inside a transaction. Returns True on success."""
    removes = [r["entity"] for r in ranked if r["entity"]["id"] != keep_entity["id"]]

    logger.info(f"\nResolving group: keep={keep_entity['name']} ({keep_entity['client_key']})")
    for rem in removes:
        logger.info(f"  → removing {rem['name']} ({rem['client_key']})")

    try:
        ensure_removed_entities_table(cur, dry_run)

        for rem in removes:
            logger.info(f"\n  Migrating {rem['name']} → {keep_entity['name']}")
            migrate_entity(cur, rem, keep_entity, dry_run)

        if dry_run:
            conn.rollback()
            print("  [DRY-RUN] transaction rolled back — no changes made.")
        else:
            conn.commit()
            print(f"  ✓ Group resolved and committed.")
        return True

    except Exception as e:
        conn.rollback()
        logger.error(f"  Error resolving group: {e}", exc_info=True)
        print(f"  ✗ Error — transaction rolled back. See resolve_duplicates.log.")
        return False


# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

def interactive_prompt(ranked: list[dict]) -> tuple[str, dict | None]:
    """
    Returns (action, keep_entity_or_None).
    action: ACCEPT | OVERRIDE | SKIP | NOT_DUPLICATE
    """
    print(f"\nOptions:")
    print(f"  [A] Accept recommendation  (keep #{1}: {ranked[0]['entity']['name']})")
    print(f"  [O] Override — choose which entity to keep")
    print(f"  [S] Skip — decide later")
    print(f"  [N] Not duplicates — mark and skip")

    while True:
        choice = input("\nYour choice [A/O/S/N]: ").strip().upper()

        if choice == "A":
            return "ACCEPT", ranked[0]["entity"]

        if choice == "O":
            print("\nEnter the number (#) of the entity to KEEP:")
            for i, r in enumerate(ranked):
                e = r["entity"]
                print(f"  {i+1}. {e['name']}  ({e['client_key'] or '—'})  appts={r['appointments']}")
            while True:
                num = input("Keep # (or paste entity ID): ").strip()
                if num.isdigit() and 1 <= int(num) <= len(ranked):
                    return "OVERRIDE", ranked[int(num) - 1]["entity"]
                # Try matching by ID or client_key
                match = next((r["entity"] for r in ranked
                              if r["entity"]["id"] == num or r["entity"]["client_key"] == num), None)
                if match:
                    return "OVERRIDE", match
                print("  Invalid — try again.")

        if choice == "S":
            return "SKIP", None

        if choice == "N":
            return "NOT_DUPLICATE", None

        print("  Please enter A, O, S, or N.")


# ---------------------------------------------------------------------------
# Batch mode
# ---------------------------------------------------------------------------

def load_batch_decisions(csv_path: str) -> dict[int, dict]:
    """
    Returns {group_number: {action, keep_entity_id}}.
    CSV columns: group_number, action, keep_entity_id (keep_entity_id optional for ACCEPT).
    """
    decisions = {}
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                gn = int(row["group_number"])
                decisions[gn] = {
                    "action":         row["action"].strip().upper(),
                    "keep_entity_id": row.get("keep_entity_id", "").strip() or None,
                }
            except (KeyError, ValueError) as e:
                logger.warning(f"Skipping malformed batch row {row}: {e}")
    logger.info(f"Loaded {len(decisions)} batch decisions from {csv_path}")
    return decisions


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--threshold", type=int, default=85,
                        help="Fuzzy similarity threshold %% (default: 85)")
    parser.add_argument("--dsn", default="dbname=test_flippro_hostke",
                        help="PostgreSQL DSN")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without modifying the database")
    parser.add_argument("--group", type=int, default=None,
                        help="Process only this group number (1-based)")
    parser.add_argument("--batch", default=None, metavar="CSV",
                        help="Path to batch decisions CSV")
    args = parser.parse_args()

    mode = "batch" if args.batch else "interactive"
    logger.info(f"Starting duplicate resolution | mode={mode} | dry_run={args.dry_run} "
                f"| threshold={args.threshold}%")

    conn = connect(args.dsn)
    cur  = conn.cursor()

    # Load data
    entities   = load_entities(cur)
    appt_map   = load_appointments(cur)
    ins_map    = load_insurance_mappings(cur)
    branch_map = load_branches(cur)

    logger.info(f"Loaded {len(entities)} entities")

    groups = find_duplicate_groups(entities, args.threshold)
    if not groups:
        print(f"No duplicate groups found at {args.threshold}% threshold.")
        cur.close(); conn.close()
        return

    logger.info(f"Found {len(groups)} duplicate group(s)")

    # Filter to a specific group if requested
    group_indices = [args.group - 1] if args.group else range(len(groups))

    # Load batch decisions if applicable
    batch = load_batch_decisions(args.batch) if args.batch else {}

    # Tracking
    results = {"resolved": 0, "skipped": 0, "not_duplicate": 0, "errors": 0}

    for idx in group_indices:
        if idx < 0 or idx >= len(groups):
            logger.warning(f"Group {idx + 1} does not exist (total: {len(groups)})")
            continue

        g_idx  = idx + 1
        grp    = groups[idx]
        ranked = rank_group(grp, appt_map, ins_map, branch_map)

        print_group_summary(g_idx, grp, ranked, args.threshold)

        # Determine action
        if mode == "batch":
            decision = batch.get(g_idx)
            if not decision:
                logger.info(f"Group {g_idx}: no batch decision — skipping")
                results["skipped"] += 1
                continue

            action         = decision["action"]
            keep_entity_id = decision["keep_entity_id"]

            if action == "ACCEPT":
                keep_entity = ranked[0]["entity"]
            elif action == "OVERRIDE":
                keep_entity = next(
                    (r["entity"] for r in ranked if r["entity"]["id"] == keep_entity_id),
                    None,
                )
                if not keep_entity:
                    logger.warning(f"Group {g_idx}: OVERRIDE entity ID not found — skipping")
                    results["skipped"] += 1
                    continue
            elif action in ("SKIP", "NOT_DUPLICATE"):
                logger.info(f"Group {g_idx}: {action}")
                results["skipped" if action == "SKIP" else "not_duplicate"] += 1
                continue
            else:
                logger.warning(f"Group {g_idx}: unknown action '{action}' — skipping")
                results["skipped"] += 1
                continue

        else:  # interactive
            action, keep_entity = interactive_prompt(ranked)

            if action == "SKIP":
                print(f"  → Skipped group {g_idx}.")
                results["skipped"] += 1
                continue
            if action == "NOT_DUPLICATE":
                print(f"  → Marked as not duplicates — skipped.")
                results["not_duplicate"] += 1
                continue

        # Confirm before executing
        removes = [r["entity"] for r in ranked if r["entity"]["id"] != keep_entity["id"]]
        print(f"\n  KEEP   → {keep_entity['name']} ({keep_entity['client_key']})")
        for rem in removes:
            print(f"  REMOVE → {rem['name']} ({rem['client_key']})")

        if args.dry_run:
            print("  [DRY-RUN] Proceeding in dry-run mode.")
        else:
            confirm = input("\n  Confirm? [y/N]: ").strip().lower()
            if confirm != "y":
                print("  Cancelled — skipping this group.")
                results["skipped"] += 1
                continue

        ok = resolve_group(conn, cur, grp, ranked, keep_entity, args.dry_run)
        results["resolved" if ok else "errors"] += 1

    # Final summary
    print(f"\n{DIVIDER}")
    print(f"  RESOLUTION COMPLETE")
    print(f"  Resolved  : {results['resolved']}")
    print(f"  Skipped   : {results['skipped']}")
    print(f"  Not dupes : {results['not_duplicate']}")
    print(f"  Errors    : {results['errors']}")
    print(f"  Log file  : resolve_duplicates.log")
    print(DIVIDER)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
