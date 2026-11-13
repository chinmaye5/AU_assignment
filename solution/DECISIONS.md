# Architecture & Ambiguity Decisions

This document tracks all intentional choices and interpretations made where the specification allowed multiple approaches.

| # | Decision | Why | Alternative Considered |
|---|---|---|---|
| 1 | HMAC-SHA256 with a secret key (`f"{workflow_id}\|{canonical_phone}"`) instead of a plain hash. | Plain SHA-256 on 10-digit Indian mobile numbers (~4 billion combinations) is trivially rainbow-table reversible; keyed HMAC prevents enumeration. | Plain SHA-256 without salt/key. |
| 2 | Store `phone_last4` separately in `leads`. | Allows fast, decryption-free rendering of masked phones (`+91******1234`) in `lead_report.csv`. | Decrypting the full phone or reconstructing from raw PII at report generation time. |
| 3 | Derive linking, orphans, and unknown dispositions at report time instead of ingest time. | Guarantees idempotency and drop-order equivalence: events arriving before their leads (drop 1 vs drop 2) link automatically without re-ingesting events. | Linking at ingest time and attempting backfill mutations on previously ingested events. |
| 4 | Malformed events = invalid JSON, JSON non-object, or empty/missing `event_id`. | Strictly preserves the append-only ledger for all parseable event structures. Events with unparseable timestamps or missing schemas stay in ledger and count as orphans at report time. | Silently dropping lines with unparseable timestamps or failing the ingest transaction. |
| 5 | Required non-phone field present but invalid rejected as `invalid_<field_name>`. | Provides deterministic, descriptive rejection feedback consistent with schema validation rules. | Generic `invalid_field` or ignoring validation on non-phone fields. |
| 6 | Two columns mapping to the same field: first column wins. | Ensures deterministic header resolution in case CSVs provide redundant or duplicate alias headers. | Last column wins or throwing an error. |
| 7 | Unknown disposition matching is case-sensitive and exact. | Prevents silent corruption from typos or unlisted statuses (e.g. `RENEWD` vs `renewed`). | Case-insensitive matching or mapping to fallback. |
| 8 | Calls without a linked `call.initiated` are not counted. | The spec defines a call as a `(workflow_id, call_id)` having a linked `call.initiated` event. | Counting orphan call fragments or connected events without initiation. |
| 9 | Multiple `call.initiated` for one call: earliest by `(occurred_at, event_id)` wins. | Provides deterministic call start timestamp even if network retries duplicated initiation events with slight delays. | Latest initiation or arbitrary first-seen order. |
| 10 | Ties between equal timestamps: larger `event_id` wins ("latest"). | Ensures total ordering and determinism when events occur within the same sub-second window. | Arbitrary database rowid order. |
| 11 | Event types outside the six known types are stored and ignored. | Maintains append-only audit ledger without crashing on upstream feature additions. | Rejecting unknown event types as malformed. |
| 12 | Batches with zero valid leads do not appear in `batch_summary.csv`. | Batch summary reflects lead cohorts; batches that produced only rejections contain no leads to summarize. | Listing batch with zeros for all lead metrics. |
| 13 | `rejections_by_reason` in `data_quality.json` lists only reasons that actually occurred, keys sorted. | Avoids cluttering summary with dozens of unused schema-specific rejection keys. | Pre-populating all hypothetical schema error keys with 0. |
| 14 | Files in the same directory are processed in plain `sorted()` filename order. | Ensures deterministic, repeatable file ingestion order across all operating systems. | Filesystem directory iterator order (`os.listdir`). |
| 15 | File skip key is `(kind, workflow_id, file_name, sha256)`. | If a file changes content, its new SHA-256 causes re-processing; identical re-runs are cleanly skipped. | Name-only check or always overwriting. |
| 16 | Dates require zero-padded day/month and a 4-digit year. | Adheres strictly to ISO and day-first standards (`YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD-Mon-YYYY`) and prevents ambiguous year representations. | Permissive dateutil parsing. |

