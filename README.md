# Duplicate Entity Resolution — test_flippro_hostke

Tools for finding, analysing, and resolving duplicate entities in the `test_flippro_hostke` PostgreSQL database.

---

## Overview

The workflow is four steps:

```
1. Analyse   →   duplicate_entities_analysis.py   (find duplicates, export Excel workplan)
2. Review    →   duplicate_report_<timestamp>.xlsx (fill in decisions)
3. Resolve   →   resolve_duplicates.py             (apply decisions to the main database)
4. Auth      →   resolve_auth_users.py             (reassign users in the auth database)
```

`resolve_duplicates.py` is fully self-contained — it can be run directly without running the analysis script first. The analysis script is only needed if you want the Excel workbook for offline review.

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
uv pip install psycopg2-binary rapidfuzz tabulate openpyxl pyyaml tqdm python-dotenv
```

> If you don't have `uv`, use `python3 -m venv .venv` and `pip install` instead.

### Database connection (.env)

All three scripts read connection strings from a `.env` file. Copy the example and fill in your details:

```bash
cp .env.example .env
```

Then edit `.env`:

```
DSN=host=<host> dbname=<db_name> user=<user> password=<password>
AUTH_DSN=host=<auth-host> dbname=<auth_db_name> user=<user> password=<password>
```

`DSN` is the main (flippro) database. `AUTH_DSN` is the auth database (used only by `resolve_auth_users.py`).

The `.env` file is gitignored and never committed. The `--dsn` / `--auth-dsn` flags on any script override the `.env` values. If a required value is not set the script exits with an error.

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

# Override the .env DSN for this run only
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
2. Migrates insurance mappings — deletes any that would conflict with an existing mapping on the kept entity (duplicate mapping is redundant)
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

For each group the summary table shows entity name, email, client key, status, appointment count, insurance mapping count, branch count, and the script's suggestion. You are then prompted:

```
[A] Accept recommendation
[O] Override — choose which entity to keep
[P] Partial  — remove some entities, keep others, choose which receives migrated data
[S] Skip     — decide later
[N] Not duplicates — mark and skip
```

**Override** — enter a row number, entity ID, or client key to select which entity to keep.

**Partial** — useful when only some entities in a group are true duplicates:
1. Enter the row numbers to remove (comma-separated, e.g. `2,3`)
2. If more than one entity is being kept, choose which one should receive the migrated data
3. Entities not selected for removal are left completely untouched

Before each group is processed a confirmation screen shows:
- `RECEIVE →` entity that receives all migrated data
- `REMOVE  →` entities being deleted
- `KEEP    →` entities left untouched (Partial mode only)

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

## Step 4 — Auth-DB cleanup

`resolve_auth_users.py` reads every entry in `removed_entities` where `resolved_in_auth_db = false` and reassigns those users in the auth database:

- If a user is already associated with the kept entity → the redundant `entity_users` row is deleted.
- If not yet associated → their `entity_id` is updated to the kept entity.
- Once a removed entity's users are all handled, `resolved_in_auth_db` is flipped to `true` in the main DB.

Each removed entity is handled in its own transaction pair (auth DB + main DB) — a failure on one entry does not block the rest.

```bash
# Interactive — preview each entry and confirm before applying
.venv/bin/python resolve_auth_users.py

# Dry run — show what would happen, touch nothing
.venv/bin/python resolve_auth_users.py --dry-run

# Non-interactive — apply all entries without prompts
.venv/bin/python resolve_auth_users.py --yes

# Override .env DSNs for this run
.venv/bin/python resolve_auth_users.py \
  --dsn "host=<host> dbname=<db> user=<user> password=<pass>" \
  --auth-dsn "host=<auth-host> dbname=<auth_db> user=<user> password=<pass>"
```

> **Always dry-run first** to confirm the right users are being moved before committing.

You can audit pending and completed entries directly in the main DB:

```sql
SELECT removed_entity_id, removed_client_key, entity_name,
       kept_entity_id, kept_client_key, removed_at, resolved_in_auth_db
FROM removed_entities
ORDER BY removed_at;
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
| `resolve_duplicates.log` | Per-group resolution activity — what was migrated, deleted, or errored |
| `resolve_auth_users.log` | Per-entry auth-DB reassignment activity |
