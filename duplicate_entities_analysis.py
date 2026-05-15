#!/usr/bin/env python3
"""
Duplicate entity analysis for test_flippro_hostke.

Finds entities with similar names using fuzzy matching, then reports
appointments, insurance mappings, and branches for each duplicate group.

Always writes an Excel workbook alongside terminal output.

Usage:
    python duplicate_entities_analysis.py [--threshold 85] [--dsn "dbname=..."] [--xlsx out.xlsx]
"""

import argparse
import sys
from collections import defaultdict
from datetime import datetime
from itertools import combinations

import psycopg2
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from rapidfuzz import fuzz
from tabulate import tabulate


# ---------------------------------------------------------------------------
# Fuzzy scoring
# ---------------------------------------------------------------------------

def similarity(a: str, b: str) -> float:
    """
    max(ratio, token_sort_ratio): strict enough to avoid false positives from
    shared industry keywords, but catches spacing/punctuation/order variants.
    """
    return max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b))


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def connect(dsn: str):
    try:
        return psycopg2.connect(dsn)
    except psycopg2.OperationalError as e:
        print(f"[ERROR] Cannot connect to database: {e}", file=sys.stderr)
        sys.exit(1)


def fetch(cur, sql: str, params=None) -> list[dict]:
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_entities(cur) -> list[dict]:
    return fetch(cur, """
        SELECT e.id, e.name, e.email, e.phone_number, e.status,
               e.client_key, e.is_onboarded, e.v_one_id,
               e.physical_address,
               et.name AS entity_type
        FROM entities e
        LEFT JOIN entity_type et ON et.id = e.entity_type_id
        ORDER BY e.name
    """)


def load_appointments(cur) -> dict[str, dict]:
    rows = fetch(cur, """
        SELECT service_provider_id::text AS eid,
               COUNT(*)                  AS total,
               COUNT(*) FILTER (WHERE task_status = 'COMPLETED')              AS completed,
               COUNT(*) FILTER (WHERE task_status = 'PENDING')                AS pending,
               COUNT(*) FILTER (WHERE task_status NOT IN ('COMPLETED','PENDING')) AS other
        FROM appointments
        WHERE service_provider_id IS NOT NULL
        GROUP BY service_provider_id
    """)
    return {r["eid"]: r for r in rows}


def load_insurance_mappings(cur) -> dict[str, list[dict]]:
    rows = fetch(cur, """
        SELECT ete.service_provider_id::text AS sp_id,
               ete.id::text                  AS mapping_id,
               ete.status                    AS mapping_status,
               ins.name                      AS insurance_name,
               ins.id::text                  AS insurance_id,
               ete.can_book_valuation,
               ete.markup,
               ete.vatable
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
        SELECT eb.entity_id::text AS eid,
               eb.id::text        AS branch_id,
               eb.email, eb.phone_number, eb.physical_address,
               eb.status,
               aa.name            AS admin_area
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

    for i, j in combinations(range(n), 2):
        score = similarity(entities[i]["name"], entities[j]["name"])
        if score >= threshold:
            adjacency[i].add(j)
            adjacency[j].add(i)

    visited = set()
    groups = []
    for start in range(n):
        if start in visited or start not in adjacency:
            continue
        group_indices = set()
        queue = [start]
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


def rank_group(group, appt_map, ins_map, branch_map):
    """Returns group members sorted best→worst (KEEP first)."""
    ranked = []
    for e in group:
        a = appt_map.get(e["id"], {})
        ranked.append({
            "entity": e,
            "appointments": a.get("total", 0),
            "insurance_mappings": len(ins_map.get(e["id"], [])),
            "branches": len(branch_map.get(e["id"], [])),
        })
    ranked.sort(key=lambda x: (
        -x["appointments"],
        -x["insurance_mappings"],
        -x["branches"],
    ))
    return ranked


# ---------------------------------------------------------------------------
# Terminal output
# ---------------------------------------------------------------------------

DIVIDER = "=" * 78


def section(title: str):
    print(f"\n{DIVIDER}\n  {title}\n{DIVIDER}")


def print_group(group, appt_map, ins_map, branch_map, threshold):
    # Similarity scores
    print("\nSimilarity scores (pairs that meet threshold):")
    pairs = []
    for a, b in combinations(group, 2):
        score = similarity(a["name"], b["name"])
        if score >= threshold:
            pairs.append([a["name"], b["name"], f"{round(score)}%"])
    print(tabulate(pairs, headers=["Entity A", "Entity B", "Score"], tablefmt="rounded_outline"))

    # 1. Entity details
    section("1. ENTITY DETAILS")
    rows = [[
        e["id"], e["name"], e["entity_type"] or "—", e["status"],
        e["client_key"] or "—", "Yes" if e["is_onboarded"] else "No",
        e["email"] or "—", e["phone_number"] or "—",
    ] for e in group]
    print(tabulate(rows, tablefmt="rounded_outline",
                   headers=["ID", "Name", "Type", "Status", "Client Key", "Onboarded", "Email", "Phone"]))

    # 2. Appointments
    section("2. APPOINTMENTS")
    rows = []
    for e in group:
        a = appt_map.get(e["id"], {})
        rows.append([e["name"], e["client_key"] or "—",
                     a.get("total", 0), a.get("completed", 0),
                     a.get("pending", 0), a.get("other", 0)])
    print(tabulate(rows, tablefmt="rounded_outline",
                   headers=["Entity", "Client Key", "Total", "Completed", "Pending", "Other"]))

    # 3. Insurance mappings
    section("3. INSURANCE MAPPINGS (entity_to_entity)")
    rows = []
    for e in group:
        mappings = ins_map.get(e["id"], [])
        if not mappings:
            rows.append([e["name"], e["client_key"] or "—", 0, "—", "—", "—"])
        for m in mappings:
            rows.append([e["name"], e["client_key"] or "—", len(mappings),
                         m["insurance_name"], m["mapping_status"],
                         "Yes" if m["can_book_valuation"] else "No"])
    print(tabulate(rows, tablefmt="rounded_outline",
                   headers=["Entity", "Client Key", "Total Mappings", "Insurance",
                             "Mapping Status", "Can Book Val."]))

    # 4. Branches
    section("4. BRANCHES")
    rows = []
    for e in group:
        branches = branch_map.get(e["id"], [])
        if not branches:
            rows.append([e["name"], e["client_key"] or "—", 0, "—", "—", "—", "—"])
        for b in branches:
            rows.append([e["name"], e["client_key"] or "—", len(branches),
                         b["admin_area"] or "—", b["status"] or "—",
                         b["email"] or "—", b["phone_number"] or "—"])
    print(tabulate(rows, tablefmt="rounded_outline",
                   headers=["Entity", "Client Key", "Total Branches",
                             "Admin Area", "Status", "Email", "Phone"]))

    # 5. Summary
    section("5. CONSOLIDATED SUMMARY & REMOVAL SUGGESTION")
    ranked = rank_group(group, appt_map, ins_map, branch_map)
    rows = []
    for i, r in enumerate(ranked):
        e = r["entity"]
        rows.append([e["name"], e["id"], e["client_key"] or "—", e["status"],
                     r["appointments"], r["insurance_mappings"], r["branches"],
                     "KEEP" if i == 0 else "REMOVE"])
    print(tabulate(rows, tablefmt="rounded_outline",
                   headers=["Name", "ID", "Client Key", "Status",
                             "Appointments", "Insurance Mappings", "Branches", "Action"]))

    keep = ranked[0]["entity"]
    print(f"\n  KEEP   → {keep['name']} (client_key={keep['client_key']}, id={keep['id']})")
    for r in ranked[1:]:
        e = r["entity"]
        print(f"  REMOVE → {e['name']} (client_key={e['client_key']}, id={e['id']})")


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------

# Palette
_HDR_FILL   = PatternFill("solid", fgColor="1F4E79")   # dark blue
_KEEP_FILL  = PatternFill("solid", fgColor="C6EFCE")   # light green
_REM_FILL   = PatternFill("solid", fgColor="FFDDC1")   # light orange
_GRP_FILL   = PatternFill("solid", fgColor="D9E1F2")   # light steel blue (group label rows)
_WHITE_FONT = Font(bold=True, color="FFFFFF")
_HDR_FONT   = Font(bold=True, color="FFFFFF")
_BOLD       = Font(bold=True)
_THIN       = Side(style="thin", color="BFBFBF")
_BORDER     = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_CENTER     = Alignment(horizontal="center", vertical="center", wrap_text=True)
_LEFT       = Alignment(horizontal="left",   vertical="center", wrap_text=True)


def _hdr_row(ws, row_num: int, values: list):
    for col, val in enumerate(values, 1):
        c = ws.cell(row=row_num, column=col, value=val)
        c.fill   = _HDR_FILL
        c.font   = _HDR_FONT
        c.border = _BORDER
        c.alignment = _CENTER


def _data_cell(ws, row_num: int, col: int, value, fill=None, bold=False, center=False):
    c = ws.cell(row=row_num, column=col, value=value)
    if fill:
        c.fill = fill
    if bold:
        c.font = _BOLD
    c.border    = _BORDER
    c.alignment = _CENTER if center else _LEFT
    return c


def _auto_width(ws, min_w=10, max_w=50):
    for col_cells in ws.columns:
        length = max(
            (len(str(c.value)) if c.value is not None else 0) for c in col_cells
        )
        ws.column_dimensions[get_column_letter(col_cells[0].column)].width = \
            max(min_w, min(length + 3, max_w))


def build_workbook(groups, appt_map, ins_map, branch_map, threshold, dsn) -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)   # remove default sheet

    _sheet_workplan(wb, groups, appt_map, ins_map, branch_map, threshold)
    _sheet_appointments(wb, groups, appt_map)
    _sheet_mappings(wb, groups, ins_map)
    _sheet_branches(wb, groups, branch_map)
    _sheet_similarity(wb, groups, threshold)

    return wb


def _sheet_workplan(wb, groups, appt_map, ins_map, branch_map, threshold):
    ws = wb.create_sheet("Workplan (Summary)")
    ws.freeze_panes = "A2"

    headers = [
        "Group #", "Group Name", "Entity ID", "Entity Name", "Type",
        "Status", "Client Key", "Onboarded", "Email", "Phone",
        "Appointments", "Insurance Mappings", "Branches",
        "Similarity %", "Recommended Action",
        # Tracker columns for the team
        "Decision", "Assignee", "Resolved", "Notes",
    ]
    _hdr_row(ws, 1, headers)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    row = 2
    for g_idx, group in enumerate(groups, 1):
        ranked = rank_group(group, appt_map, ins_map, branch_map)

        # Best similarity score within this group
        best_score = max(
            round(similarity(a["name"], b["name"]))
            for a, b in combinations(group, 2)
        ) if len(group) > 1 else 100

        for i, r in enumerate(ranked):
            e      = r["entity"]
            action = "KEEP" if i == 0 else "REMOVE"
            fill   = _KEEP_FILL if action == "KEEP" else _REM_FILL

            vals = [
                g_idx, group[0]["name"], e["id"], e["name"],
                e["entity_type"] or "", e["status"], e["client_key"] or "",
                "Yes" if e["is_onboarded"] else "No",
                e["email"] or "", e["phone_number"] or "",
                r["appointments"], r["insurance_mappings"], r["branches"],
                f"{best_score}%", action,
                "", "", "", "",   # tracker blanks
            ]
            for col, val in enumerate(vals, 1):
                _data_cell(ws, row, col, val, fill=fill,
                           bold=(col == 15), center=(col in {1, 11, 12, 13, 14, 15}))
            row += 1

    _auto_width(ws)
    # Lock tracker columns width
    for col_letter in ["P", "Q", "R", "S"]:
        ws.column_dimensions[col_letter].width = 18


def _sheet_appointments(wb, groups, appt_map):
    ws = wb.create_sheet("Appointments")
    ws.freeze_panes = "A2"

    headers = ["Group #", "Group Name", "Entity ID", "Entity Name",
               "Client Key", "Total", "Completed", "Pending", "Other"]
    _hdr_row(ws, 1, headers)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    row = 2
    for g_idx, group in enumerate(groups, 1):
        ranked = rank_group(group, appt_map, {}, {})
        for i, r in enumerate(ranked):
            e    = r["entity"]
            a    = appt_map.get(e["id"], {})
            fill = _KEEP_FILL if i == 0 else _REM_FILL
            for col, val in enumerate([
                g_idx, group[0]["name"], e["id"], e["name"], e["client_key"] or "",
                a.get("total", 0), a.get("completed", 0),
                a.get("pending", 0), a.get("other", 0),
            ], 1):
                _data_cell(ws, row, col, val, fill=fill,
                           center=(col in {1, 6, 7, 8, 9}))
            row += 1

    _auto_width(ws)


def _sheet_mappings(wb, groups, ins_map):
    ws = wb.create_sheet("Insurance Mappings")
    ws.freeze_panes = "A2"

    headers = ["Group #", "Group Name", "Entity ID", "Entity Name",
               "Client Key", "Total Mappings", "Insurance Company",
               "Mapping ID", "Mapping Status", "Can Book Valuation",
               "Markup", "Vatable"]
    _hdr_row(ws, 1, headers)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    row = 2
    for g_idx, group in enumerate(groups, 1):
        ranked = rank_group(group, {}, ins_map, {})
        for i, r in enumerate(ranked):
            e        = r["entity"]
            mappings = ins_map.get(e["id"], [])
            fill     = _KEEP_FILL if i == 0 else _REM_FILL

            if not mappings:
                for col, val in enumerate([
                    g_idx, group[0]["name"], e["id"], e["name"],
                    e["client_key"] or "", 0, "—", "—", "—", "—", "—", "—",
                ], 1):
                    _data_cell(ws, row, col, val, fill=fill, center=(col in {1, 6}))
                row += 1
            else:
                for m in mappings:
                    for col, val in enumerate([
                        g_idx, group[0]["name"], e["id"], e["name"],
                        e["client_key"] or "", len(mappings),
                        m["insurance_name"], m["mapping_id"], m["mapping_status"],
                        "Yes" if m["can_book_valuation"] else "No",
                        m["markup"], "Yes" if m["vatable"] else "No",
                    ], 1):
                        _data_cell(ws, row, col, val, fill=fill,
                                   center=(col in {1, 6, 9, 10, 11, 12}))
                    row += 1

    _auto_width(ws)


def _sheet_branches(wb, groups, branch_map):
    ws = wb.create_sheet("Branches")
    ws.freeze_panes = "A2"

    headers = ["Group #", "Group Name", "Entity ID", "Entity Name",
               "Client Key", "Total Branches", "Branch ID",
               "Admin Area", "Status", "Email", "Phone", "Address"]
    _hdr_row(ws, 1, headers)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    row = 2
    for g_idx, group in enumerate(groups, 1):
        ranked = rank_group(group, {}, {}, branch_map)
        for i, r in enumerate(ranked):
            e        = r["entity"]
            branches = branch_map.get(e["id"], [])
            fill     = _KEEP_FILL if i == 0 else _REM_FILL

            if not branches:
                for col, val in enumerate([
                    g_idx, group[0]["name"], e["id"], e["name"],
                    e["client_key"] or "", 0, "—", "—", "—", "—", "—", "—",
                ], 1):
                    _data_cell(ws, row, col, val, fill=fill, center=(col in {1, 6}))
                row += 1
            else:
                for b in branches:
                    for col, val in enumerate([
                        g_idx, group[0]["name"], e["id"], e["name"],
                        e["client_key"] or "", len(branches),
                        b["branch_id"], b["admin_area"] or "—",
                        b["status"] or "—", b["email"] or "—",
                        b["phone_number"] or "—", b["physical_address"] or "—",
                    ], 1):
                        _data_cell(ws, row, col, val, fill=fill, center=(col in {1, 6, 9}))
                    row += 1

    _auto_width(ws)


def _sheet_similarity(wb, groups, threshold):
    ws = wb.create_sheet("Similarity Scores")
    ws.freeze_panes = "A2"

    headers = ["Group #", "Group Name", "Entity A", "Entity B", "Score %", "Meets Threshold"]
    _hdr_row(ws, 1, headers)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    row = 2
    for g_idx, group in enumerate(groups, 1):
        for a, b in combinations(group, 2):
            score = round(similarity(a["name"], b["name"]))
            meets = score >= threshold
            fill  = _KEEP_FILL if meets else None
            for col, val in enumerate([
                g_idx, group[0]["name"], a["name"], b["name"],
                f"{score}%", "Yes" if meets else "No",
            ], 1):
                _data_cell(ws, row, col, val, fill=fill, center=(col in {1, 5, 6}))
            row += 1

    _auto_width(ws)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=int, default=85,
                        help="Fuzzy similarity threshold %% (default: 85)")
    parser.add_argument("--dsn", default="dbname=test_flippro_hostke",
                        help="PostgreSQL DSN (default: dbname=test_flippro_hostke)")
    parser.add_argument("--xlsx", default=None,
                        help="Excel output path (default: duplicate_report_<timestamp>.xlsx)")
    parser.add_argument("--no-terminal", action="store_true",
                        help="Suppress terminal table output (Excel only)")
    args = parser.parse_args()

    xlsx_path = args.xlsx or f"duplicate_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    conn = connect(args.dsn)
    cur  = conn.cursor()

    print(f"\nConnected  : {args.dsn}")
    print(f"Threshold  : {args.threshold}%  (max of ratio + token_sort_ratio)")
    print(f"Excel out  : {xlsx_path}")

    entities   = load_entities(cur)
    appt_map   = load_appointments(cur)
    ins_map    = load_insurance_mappings(cur)
    branch_map = load_branches(cur)

    print(f"Entities   : {len(entities)} loaded — scanning for duplicates...\n")

    groups = find_duplicate_groups(entities, args.threshold)

    if not groups:
        print(f"No duplicate groups found at {args.threshold}% threshold.")
        cur.close(); conn.close()
        return

    print(f"Found {len(groups)} duplicate group(s).\n")

    if not args.no_terminal:
        for idx, group in enumerate(groups, 1):
            print(f"\n{'#' * 78}")
            print(f"  DUPLICATE GROUP {idx} of {len(groups)}:  {group[0]['name']}")
            print(f"{'#' * 78}")
            print_group(group, appt_map, ins_map, branch_map, args.threshold)

    print(f"\nBuilding Excel workbook...")
    wb = build_workbook(groups, appt_map, ins_map, branch_map, args.threshold, args.dsn)
    wb.save(xlsx_path)
    print(f"Saved → {xlsx_path}")

    cur.close()
    conn.close()
    print(f"\n{DIVIDER}\n  Analysis complete.\n{DIVIDER}")


if __name__ == "__main__":
    main()
