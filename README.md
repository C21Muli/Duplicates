# Duplicate Entity Resolution — test_flippro_hostke

Tools for finding, analysing, and resolving duplicate entities in the `test_flippro_hostke` PostgreSQL database.

---

## Overview

The workflow is three steps:

```
1. Analyse   →   duplicate_entities_analysis.py   (find duplicates, export Excel workplan)
2. Review    →   duplicate_report_<timestamp>.xlsx (fill in decisions)
3. Resolve   →   resolve_duplicates.py             (apply decisions to the database)
```

A plain-SQL audit script (`duplicate_entities_analysis.sql`) and a SQL workplan (`duplicate_entities_workplan.sql`) are also included for one-off use or reference.

---

## Setup

Requires Python 3.12+ and [uv](https://github.com/astral-sh/uv).

```bash
# Clone the repo
git clone https://github.com/C21Muli/Duplicates.git
cd Duplicates

# Create virtual environment and install dependencies
uv venv
uv pip install psycopg2-binary rapidfuzz tabulate openpyxl pyyaml tqdm
```

> If you don't have `uv`, use `python3 -m venv .venv` and `pip install` instead.

---

## Step 1 — Analyse

`duplicate_entities_analysis.py` connects to the database, fuzzy-matches entity names, and produces both a terminal report and an Excel workbook.

```bash
# Default (85% similarity threshold, outputs duplicate_report_<timestamp>.xlsx)
.venv/bin/python duplicate_entities_analysis.py

# Excel only — no terminal noise
.venv/bin/python duplicate_entities_analysis.py --no-terminal

# Stricter threshold — exact/near-exact duplicates only
.venv/bin/python duplicate_entities_analysis.py --threshold 95 --no-terminal

# Custom output file
.venv/bin/python duplicate_entities_analysis.py --xlsx workplan.xlsx

# Remote database
.venv/bin/python duplicate_entities_analysis.py \
  --dsn "host=<host> dbname=test_flippro_hostke user=<user> password=<pass>"

# Preview only — no Excel file written
.venv/bin/python duplicate_entities_analysis.py --dry-run
```

### How duplicates are detected

Names are compared using `max(ratio, token_sort_ratio)` on both:
- the normalised name (`&` → `and`, punctuation stripped, lowercase)
- the name with legal suffixes removed (`LTD`, `LIMITED`, `LLP`, etc.)

This catches spacing variants (`WESTEND` vs `WEST END`), punctuation variants (`&` vs `AND`), and suffix variants (`ABC LTD` vs `ABC LIMITED`) without producing false positives from shared industry keywords like `GARAGE LTD`.

### Excel workbook sheets

| Sheet | Contents |
|---|---|
| **Workplan (Summary)** | One row per entity — `KEEP` (green) / `REMOVE` (orange), all counts, similarity %, plus blank **Decision / Assignee / Resolved / Notes** columns for the team |
| **Appointments** | Total / completed / pending / other per entity |
| **Insurance Mappings** | Insurer name, mapping status, can-book-valuation, markup, vatable |
| **Branches** | Admin area, status, contact details per branch |
| **Similarity Scores** | Pairwise scores for all pairs within each group |

---

## Step 2 — Review

Open the generated Excel workbook and fill in the **Decision** column on the **Workplan (Summary)** sheet for each group:

| Decision value | Meaning |
|---|---|
| `ACCEPT` | Use the script's recommendation (entity with most appointments / mappings / branches is kept) |
| `OVERRIDE` | You will specify which entity to keep manually |
| `SKIP` | Defer this group — do not process it yet |
| `NOT_DUPLICATE` | These are distinct entities — skip permanently |

When ready, export the **Workplan (Summary)** sheet to CSV for use in Step 3.

---

## Step 3 — Resolve

`resolve_duplicates.py` applies decisions to the database. For each removed entity it:

1. Migrates appointments (`service_provider_id`) to the kept entity
2. Migrates insurance mappings — skips any that would conflict with an existing mapping on the kept entity
3. Reassigns branches to the kept entity
4. Reassigns entity LOBs and dashboard cards
5. Re-parents any child entities (`parent_entity_id`)
6. Logs the removed entity to the `removed_entities` table (for auth-DB cleanup)
7. Deletes the removed entity (`entity_entity_types` is cleaned up automatically via CASCADE)

Each group runs in its own transaction — a failure rolls back only that group.

### Interactive mode (recommended for first run)

```bash
.venv/bin/python resolve_duplicates.py
```

For each group you are prompted:

```
[A] Accept recommendation
[O] Override — choose which entity to keep
[S] Skip — decide later
[N] Not duplicates — mark and skip
```

Override lets you type a row number, entity ID, or client key to select which entity to keep.

### Batch mode (after filling in the Excel workplan)

Export the **Workplan (Summary)** sheet to `decisions.csv` with at least these columns:

```csv
group_number,action,keep_entity_id
1,ACCEPT,
2,OVERRIDE,ea736d14-fd42-4dbb-97e0-f871b2094f96
3,SKIP,
4,NOT_DUPLICATE,
```

`keep_entity_id` is only required for `OVERRIDE` rows.

```bash
.venv/bin/python resolve_duplicates.py --batch decisions.csv
```

### Other flags

```bash
# Dry run — preview all changes without touching the database
.venv/bin/python resolve_duplicates.py --dry-run
.venv/bin/python resolve_duplicates.py --dry-run --batch decisions.csv

# Process a single group (by number from the analysis output)
.venv/bin/python resolve_duplicates.py --group 5

# Different threshold or DSN
.venv/bin/python resolve_duplicates.py --threshold 90 \
  --dsn "host=<host> dbname=test_flippro_hostke user=<user> password=<pass>"
```

> **Always dry-run first.** Review the log output before committing to a real run.

---

## Auth-DB cleanup

Every entity that is deleted is recorded in the `removed_entities` table:

```sql
SELECT removed_entity_id, removed_client_key, entity_name,
       kept_entity_id, kept_client_key, removed_at, resolved_in_auth_db
FROM removed_entities
ORDER BY removed_at;
```

Once the corresponding users in the `auth` database have been migrated or reassigned, flip the flag:

```sql
UPDATE removed_entities
SET resolved_in_auth_db = true
WHERE removed_entity_id = '<uuid>';
```

---

## SQL scripts (reference / one-off use)

| File | Purpose |
|---|---|
| `duplicate_entities_analysis.sql` | Read-only audit — 6 sections covering duplicates, appointments, mappings, branches, and removal candidates. Safe to run anytime. |
| `duplicate_entities_workplan.sql` | Transactional workplan for the BHATTI PANEL BEATERS LTD case specifically. Runs inside a single `BEGIN` block — call `COMMIT` or `ROLLBACK` at the end. |

```bash
# Audit
psql -d test_flippro_hostke -f duplicate_entities_analysis.sql

# Workplan (dry run — review output, then COMMIT or ROLLBACK)
psql -d test_flippro_hostke -f duplicate_entities_workplan.sql
```

---

## Logs

| File | Contents |
|---|---|
| `duplicate_analysis.log` | Output from the last analysis run |
| `resolve_duplicates.log` | Per-group resolution activity — what was migrated, skipped, or errored |
