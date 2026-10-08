# Internal task review storage

Reviews are hosted, organization-scoped feedback, separate from automated QA.
They bind to a task version **and the server-computed archive fingerprint**.
The uploader's `task_versions.content_hash` is not authoritative. If a version
is overwritten, its old reviews must not contribute to the new content's rating.

Two tables are introduced by backend migration `task_reviews_001`:

| Column | Type | Purpose |
| --- | --- | --- |
| id | varchar(36), primary key | Application-generated UUID |
| org_id | varchar(64), required FK | Organization; hard deletion cascades |
| reviewer_id | varchar(64), required FK | Internal user membership; hard deletion cascades |
| task_version_id | varchar(160), required FK | Version; hard deletion cascades |
| content_hash | varchar(64), required | Snapshot of server-verified fingerprint |
| content_algorithm | varchar(64), required | Fingerprint algorithm |
| revision | integer, positive, default 1 | Optimistic edit concurrency |
| created_at / updated_at | timestamptz, required | Creation and latest edit; API updates explicitly |
| deleted_at | nullable timestamptz | Soft-cleared rating or deleted comment |

`task_review_ratings` adds `score smallint CHECK (score BETWEEN 1 AND 5)`.
A unique key on `(org_id, task_version_id, content_algorithm, content_hash,
reviewer_id)` preserves one rating slot even after soft clearing. Restoring a
rating updates that slot with the expected revision; it cannot create duplicates.

`task_review_comments` adds:

- `body text`: trimmed length 1–10,000 characters.
- `client_request_id varchar(36)`: UUID validated by the API; unique per org and
  reviewer so a retried post cannot duplicate a comment.
- `source_trial_id varchar(160)`: optional trial reference; hard trial deletion
  sets it to NULL and retains the comment.

A partial comments index on `(org_id, task_version_id, content_algorithm,
content_hash, created_at, id)` supports chronological cursor pagination of live
comments. The rating unique index supports snapshot-scoped summaries.

The API must enforce tenant membership, version ownership, matching trial
ownership and verified fingerprint freshness before writing. Separate foreign
keys do not themselves establish cross-table tenant equivalence. Reads explicitly
exclude soft-deleted rows. No API or share route is exposed by this storage PR.

Apply core migrations first, then backend migrations. Upgrade is additive and
requires no backfill. Downgrade drops these two tables and their review data;
roll application code back first if a downgrade is ever needed. Soft task/version
deletion retains reviews but the API must reject access to deleted subjects.
