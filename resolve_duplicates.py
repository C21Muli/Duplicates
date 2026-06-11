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
    --dsn "..."        PostgreSQL DSN (or set DSN= in .env).

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
import os
import re
import sys
from collections import defaultdict
from itertools import combinations

import psycopg2
from dotenv import load_dotenv
from rapidfuzz import fuzz
from tabulate import tabulate
from tqdm import tqdm

load_dotenv()

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

# Emails assigned to on-the-fly entity creation — not a real business email,
# so a shared address here does NOT indicate a duplicate.
_GARBAGE_EMAILS = frozenset({
    "development@innovexsolutions.co.ke",
})


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


def _real_email(e: dict) -> str:
    """Return lowercased email if it's a real business address, else empty string."""
    addr = (e.get("email") or "").strip().lower()
    return addr if addr and addr not in _GARBAGE_EMAILS else ""


def email_match(a: dict, b: dict) -> bool:
    ea, eb = _real_email(a), _real_email(b)
    return bool(ea and ea == eb)


def entity_similarity(a: dict, b: dict) -> float:
    """Name similarity, boosted to 100 when both entities share a real email."""
    if email_match(a, b):
        return 100.0
    return similarity(a["name"], b["name"])


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _safe_dsn(dsn: str) -> str:
    return re.sub(r"(password\s*=\s*)\S+", r"\1***", dsn, flags=re.IGNORECASE)


def connect(dsn: str):
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = False
        logger.info(f"Connected to: {_safe_dsn(dsn)}")
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
    """
    Complete-linkage clustering: an entity joins a group only if it meets the
    threshold with every existing member. This prevents chain-linking where
    A≈B and B≈C at 60% pulls unrelated A and C into the same group.
    Pairs are processed strongest-first so high-confidence pairs form group cores.
    """
    n = len(entities)
    if n < 2:
        return []

    # Collect all pairs above threshold with their scores.
    sim_pairs: dict[tuple[int, int], float] = {}
    total = n * (n - 1) // 2
    with tqdm(total=total, desc="Scanning for duplicates", unit="pair") as pbar:
        for i, j in combinations(range(n), 2):
            s = entity_similarity(entities[i], entities[j])
            if s >= threshold:
                sim_pairs[(i, j)] = s  # i < j always (combinations guarantees this)
            pbar.update(1)

    # Complete-linkage clustering — process strongest pairs first.
    group_id: dict[int, int] = {}
    members:  dict[int, set[int]] = {}
    next_gid  = 0

    def all_connected(idx: int, grp: set[int]) -> bool:
        return all((min(idx, m), max(idx, m)) in sim_pairs for m in grp)

    for (i, j) in sorted(sim_pairs, key=lambda p: -sim_pairs[p]):
        gi, gj = group_id.get(i), group_id.get(j)

        if gi is None and gj is None:
            members[next_gid] = {i, j}
            group_id[i] = group_id[j] = next_gid
            next_gid += 1

        elif gi is None:
            if all_connected(i, members[gj]):
                members[gj].add(i)
                group_id[i] = gj

        elif gj is None:
            if all_connected(j, members[gi]):
                members[gi].add(j)
                group_id[j] = gi

        elif gi != gj:
            # Merge only when every cross-group pair meets threshold.
            if all((min(a, b), max(a, b)) in sim_pairs
                   for a in members[gi] for b in members[gj]):
                keep, drop = (gi, gj) if len(members[gi]) >= len(members[gj]) else (gj, gi)
                for m in members[drop]:
                    group_id[m] = keep
                members[keep].update(members.pop(drop))

    return [
        [entities[i] for i in sorted(grp)]
        for grp in members.values()
        if len(grp) >= 2
    ]


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
    pairs = []
    for a, b in combinations(group, 2):
        name_score = round(similarity(a["name"], b["name"]))
        em = email_match(a, b)
        if entity_similarity(a, b) >= threshold:
            pairs.append([a["name"], b["name"], f"{name_score}%",
                          "yes" if em else ""])
    if pairs:
        print("\nSimilarity (pairs meeting threshold):")
        print(tabulate(pairs, headers=["Entity A", "Entity B", "Name Score", "Email Match"],
                       tablefmt="rounded_outline"))

    # Consolidated summary
    rows = []
    for i, r in enumerate(ranked):
        e = r["entity"]
        rows.append([
            i + 1,
            e["name"],
            e["email"] or "—",
            e["client_key"] or "—",
            e["status"],
            r["appointments"],
            r["insurance_mappings"],
            r["branches"],
            "KEEP ✓" if i == 0 else "REMOVE",
        ])
    print()
    print(tabulate(rows, tablefmt="rounded_outline", headers=[
        "#", "Name", "Email", "Client Key", "Status",
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
            # Duplicate mapping — delete it so the FK doesn't block entity deletion.
            execute(cur,
                "DELETE FROM entity_to_entity WHERE id = %s",
                (m["id"],), dry_run)
            logger.info(f"  Mapping {m['id']} deleted — kept entity already mapped to this insurer")
            skipped_maps += 1
        else:
            execute(cur,
                "UPDATE entity_to_entity SET service_provider_id = %s WHERE id = %s",
                (kid, m["id"]), dry_run)
            existing_insurers.add(m["insurance_id"])
            migrated_maps += 1

    summary["insurance_mappings_migrated"] = migrated_maps
    summary["insurance_mappings_deleted"]  = skipped_maps
    logger.info(f"  Insurance mappings migrated: {migrated_maps}, deleted (conflict): {skipped_maps}")

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
    ranked: list[dict],
    keep_entity: dict,
    dry_run: bool,
    removes: list[dict] | None = None,
) -> bool:
    """Run full migration for one group inside a transaction. Returns True on success."""
    if removes is None:
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

def _list_entities(ranked: list[dict]):
    for i, r in enumerate(ranked):
        e = r["entity"]
        email = e["email"] or "—"
        print(f"  {i+1}. {e['name']}  ({e['client_key'] or '—'})  email={email}  appts={r['appointments']}")


def interactive_prompt(ranked: list[dict]) -> tuple[str, dict | list | None]:
    """
    Returns (action, payload).
      ACCEPT        → payload = keep_entity
      OVERRIDE      → payload = keep_entity
      PARTIAL       → payload = {"receiver": entity, "removes": [entity, ...]}
      SKIP          → payload = None
      NOT_DUPLICATE → payload = None
    """
    print(f"\nOptions:")
    print(f"  [A] Accept recommendation  (keep #1: {ranked[0]['entity']['name']})")
    print(f"  [O] Override — choose which entity to keep")
    print(f"  [P] Partial — remove some, keep others, designate data receiver")
    print(f"  [S] Skip — decide later")
    print(f"  [N] Not duplicates — mark and skip")

    while True:
        choice = input("\nYour choice [A/O/P/S/N]: ").strip().upper()

        if choice == "A":
            return "ACCEPT", ranked[0]["entity"]

        if choice == "O":
            print("\nEnter the number (#) of the entity to KEEP:")
            _list_entities(ranked)
            while True:
                num = input("Keep # (or paste entity ID): ").strip()
                if num.isdigit() and 1 <= int(num) <= len(ranked):
                    return "OVERRIDE", ranked[int(num) - 1]["entity"]
                match = next((r["entity"] for r in ranked
                              if r["entity"]["id"] == num or r["entity"]["client_key"] == num), None)
                if match:
                    return "OVERRIDE", match
                print("  Invalid — try again.")

        if choice == "P":
            print("\nWhich entities should be REMOVED? (comma-separated numbers)")
            _list_entities(ranked)
            while True:
                raw = input("Remove #s: ").strip()
                try:
                    remove_nums = [int(x.strip()) for x in raw.split(",") if x.strip()]
                except ValueError:
                    print("  Enter comma-separated numbers.")
                    continue
                if not remove_nums:
                    print("  Enter at least one number.")
                    continue
                if any(n < 1 or n > len(ranked) for n in remove_nums):
                    print(f"  Numbers must be between 1 and {len(ranked)}.")
                    continue
                if len(set(remove_nums)) >= len(ranked):
                    print("  At least one entity must be kept.")
                    continue
                break

            remove_indices = {n - 1 for n in set(remove_nums)}
            removes  = [ranked[i]["entity"] for i in remove_indices]
            kept     = [ranked[i] for i in range(len(ranked)) if i not in remove_indices]

            if len(kept) == 1:
                receiver = kept[0]["entity"]
                print(f"\n  Receiver (only kept entity): {receiver['name']}")
            else:
                print("\nWhich entity should RECEIVE migrated data? (others are kept untouched)")
                for i, r in enumerate(kept):
                    e = r["entity"]
                    email = e["email"] or "—"
                    print(f"  {i+1}. {e['name']}  ({e['client_key'] or '—'})  email={email}  appts={r['appointments']}")
                while True:
                    num = input("Receiver #: ").strip()
                    if num.isdigit() and 1 <= int(num) <= len(kept):
                        receiver = kept[int(num) - 1]["entity"]
                        break
                    print("  Invalid — try again.")

            return "PARTIAL", {"receiver": receiver, "removes": removes}

        if choice == "S":
            return "SKIP", None

        if choice == "N":
            return "NOT_DUPLICATE", None

        print("  Please enter A, O, P, S, or N.")


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
    parser.add_argument("--dsn", default=os.getenv("DSN"),
                        help="PostgreSQL DSN (or set DSN in .env)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without modifying the database")
    parser.add_argument("--group", type=int, default=None,
                        help="Process only this group number (1-based)")
    parser.add_argument("--batch", default=None, metavar="CSV",
                        help="Path to batch decisions CSV")
    args = parser.parse_args()

    if not args.dsn:
        parser.error("No DSN provided. Set DSN in .env or pass --dsn.")

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

            removes_list = None  # batch mode always removes all non-keep entities
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
            action, payload = interactive_prompt(ranked)

            if action == "SKIP":
                print(f"  → Skipped group {g_idx}.")
                results["skipped"] += 1
                continue
            if action == "NOT_DUPLICATE":
                print(f"  → Marked as not duplicates — skipped.")
                results["not_duplicate"] += 1
                continue

            if action == "PARTIAL":
                keep_entity  = payload["receiver"]
                removes_list = payload["removes"]
            else:  # ACCEPT or OVERRIDE
                keep_entity  = payload
                removes_list = None  # resolve_group will compute all non-keep entities

        # Confirm before executing
        if removes_list is None:
            removes_list = [r["entity"] for r in ranked if r["entity"]["id"] != keep_entity["id"]]

        remove_ids = {e["id"] for e in removes_list}
        print()
        for r in ranked:
            e = r["entity"]
            if e["id"] == keep_entity["id"]:
                print(f"  RECEIVE → {e['name']} ({e['client_key'] or '—'})  ← migrated data lands here")
            elif e["id"] in remove_ids:
                print(f"  REMOVE  → {e['name']} ({e['client_key'] or '—'})")
            else:
                print(f"  KEEP    → {e['name']} ({e['client_key'] or '—'})  ← untouched")

        if args.dry_run:
            print("  [DRY-RUN] Proceeding in dry-run mode.")
        else:
            confirm = input("\n  Confirm? [y/N]: ").strip().lower()
            if confirm != "y":
                print("  Cancelled — skipping this group.")
                results["skipped"] += 1
                continue

        ok = resolve_group(conn, cur, ranked, keep_entity, args.dry_run, removes=removes_list)
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
