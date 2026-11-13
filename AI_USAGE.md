# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
- **Claude (Anthropic)**: Used for high-level spec analysis, architectural trade-off discussions, brainstorming data modelling options, and drafting detailed milestone prompts.
- **Antigravity**: Used as the primary agentic coding environment to scaffold the repository, implement modules milestone-by-milestone, run tests, and assist with performance profiling.

## What I used them for
- **Spec Analysis & Requirements Breakdown**: Parsing through `assignment-a-data/README.md`, schema JSONs, and `generate_data.py` to identify subtle edge cases (e.g. out-of-order drop delivery, missing phone fields on supervisor overrides, IST midnight boundary conditions).
- **Prompt Generation & Milestone Planning**: Structuring the implementation into 7 distinct milestones (M0 scaffold to M6 polish) with strict validation criteria at each step.
- **Code Scaffolding & Initial Implementations**: Generating initial module scaffolds for `solution/parsing.py`, `solution/pii.py`, `solution/db.py`, `solution/leads.py`, `solution/events.py`, and `solution/reports.py`.
- **Test Suite Drafting**: Generating unit test cases for phone normalization variations, strict date formats, and currency strings.

## What I wrote or decided myself
- **Core Architecture Invariant**: Established the foundational principle: **store facts at ingest time, derive meaning at report time**. When considering how to handle out-of-order drops and supervisor overrides, I explicitly chose not to mutate past database rows or perform complex ingest-time backfills, keeping the ledger purely append-only and doing call aggregation dynamically at report time.
- **Supervisor Override Resolution Strategy**: Identified that `disposition.override` records carry no phone number and decided they must link strictly via `(workflow_id, call_id)` to other events in the same call session.
- **PII Security Strategy**: Decided on keyed HMAC-SHA256 incorporating `workflow_id` (`f"{workflow_id}|{canonical_phone}"`) rather than a simple salt or plain hash, preventing rainbow-table enumeration across the bounded Indian mobile number space (~4B combinations), paired with authenticated Fernet encryption for names and attributes.
- **Hand-Derived Test Expectations**: Manually calculated all expected values in `test_domain.py` and `test_parsing.py` directly from the written specification rather than allowing AI to generate assertions from its own code output.
- **Dual-Pipeline Oracle Validation**: Decided to write an independent, single-pass in-memory oracle (`tests/oracle.py`) with zero SQL dependencies to cross-verify output reports byte-for-byte across multiple random seeds (1, 2, 3, 7, 42).

## Where the AI was wrong or unhelpful
- **Ledger Mutation Proposal**: When first discussing how to handle supervisor overrides (which have no phone number), the AI suggested running an `UPDATE` on the events table to backfill `phone_hash` once the initial call event was found. I rejected this because it completely violated the append-only ledger constraint and would have triggered SQLite's `events_no_update` abort trigger. I redirected the approach to keep `phone_hash` as `NULL` on overrides and resolve the call linkage purely at report time.
- **CSV Parser Splitting on Unquoted Currency Values**: In early test scaffolding, test lead CSVs with currency values containing commas (e.g. `₹25,000`) were written unquoted. Python's `csv.reader` split the cell at the comma, resulting in `₹25` being parsed as `25.00`. I caught this during test execution, fixed the test data quoting, and ensured our `parse_decimal` utility explicitly stripped thousands separators after regex verification.
- **HMAC Object Instantiation Overhead**: During large-scale dataset benchmarking (`--scale large`, ~140k leads), the AI's initial implementation called `hmac.new()` afresh on every individual lead row. This created substantial overhead. I noticed this bottleneck during profiling and introduced a reusable `PhoneHasher` class that initializes the base HMAC once and uses `.copy()` for per-row hashing, improving throughput significantly.

## How I verified AI-generated code
- **Line-by-Line Code Review**: Inspected every generated module to ensure `parsing.py` and `domain.py` remained strictly pure functions (zero file I/O, zero global variables, zero database dependencies) so they are easy to explain and adapt during the live interview.
- **Test-Driven Invariant Checks**: Ran a comprehensive 48-test pytest suite covering phone normalisation, date parsing, half-up rounding, timezone conversions, idempotency, and database trigger enforcement.
- **Cross-Seed Oracle Verification**: Tested the pipeline against the independent reference oracle on seeds 1, 2, 3, 7, and 42 across all 5 report files, ensuring identical byte outputs regardless of drop processing order.
- **Database & Binary Inspection**: Built a dedicated `verify` CLI command and ran direct SQLite queries to confirm that triggers block `UPDATE`/`DELETE` operations, lead count math holds, and no plaintext customer names or raw phone numbers exist in the database file or reports.
