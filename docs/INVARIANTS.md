# Invariants

Code comments in this repository cite stable invariant IDs instead of repeating the reasoning.
This file is the glossary for those IDs. It is short on purpose: an invariant is a rule the code
must not break, not an essay.

## How to read a citation

- `INV-nn` — a product invariant. Violating one is a release blocker.
- `EINV-nn` — an engineering invariant. Violating one is a data-integrity or correctness bug.
- `F4`, `F5`, `F6`, `F8`, `P6` — phase identifiers. They name the change that introduced a
  behaviour, so a reader can find why it exists.
- Other identifier families appear in code comments as markers of the project's own unpublished
documents: `WT-BACKEND §n`, `BE spec §n`, `ARCH §n`, `DP-*`, `BE-CMD-*`, `BE-ENUM-*`,
  `CUTOVER-T*`, `ACCESS-T*`, `GATE-*`, `PHASE-*`, `I5`, `plan §n`, `F8-9`, bare ledger markers such
  as `G4`/`H1`/`M2`/`E8`, and defect-history markers such as `review R3` or `review round 2`. They
  record where a decision was argued, which phase introduced a behaviour, or which review caught a
  defect. None of them is a link, and none is needed to read the code: each comment states the
  reason in its own words.

## Product invariants

| ID | Rule |
|---|---|
| `INV-01` | The external HR system is an absence and day-status system, not the working-time ledger. |
| `INV-02` | Actual work is zero to many timestamped intervals per day, and intervals may cross midnight. |
| `INV-03` | Work type belongs to the interval, so a day may mix on-site and remote work. |
| `INV-04` | Detailed employee working time is private from HR and supervisors by default. |
| `INV-05` | HR, works-council and payroll access is explicit, purpose-scoped, recipient-scoped and server-authorised. |
| `INV-06` | Automatic compliance findings help the employee first and never auto-expose data to HR. |
| `INV-07` | Contract, FTE, weekday schedule and master data are effective-dated, not re-entered monthly. |
| `INV-08` | Previous balances are derived from ledger state, not copied forward by hand after cutover. |
| `INV-09` | Planned shifts, actual work intervals and shift deviations are separate objects. |
| `INV-10` | On-call availability and actual call-out work are separate objects. |
| `INV-11` | Submitted or approved historical results stay reproducible under the rules that applied then. |
| `INV-12` | A spreadsheet is a regression oracle and archive, never runtime architecture or unquestioned policy authority. |
| `INV-13` | Rollout is a clean month-boundary cutover; historical months stay in the archive. |
| `INV-14` | One transactional application plus one PostgreSQL database. |
| `INV-15` | The replacement must not silently make the regime stricter, more surveillant or more visible to HR. |

## Engineering invariants

| ID | Rule |
|---|---|
| `EINV-01` | Detailed working-time data is private by default. |
| `EINV-02` | Authentication identity binding is not ordinary profile data. |
| `EINV-03` | One logical version has at most one successor. |
| `EINV-04` | Exactly one authoritative current version per logical fact. |
| `EINV-05` | One employee's current work intervals do not overlap. |
| `EINV-06` | State transitions are atomic. |
| `EINV-07` | Frozen submissions and disclosures never silently mutate. |
| `EINV-08` | Unknown policy never becomes guessed behaviour. |
| `EINV-09` | A historical calculation resolves a specific executable policy edition. |
| `EINV-10` | Calendar classification is effective-dated data, not code. |
| `EINV-11` | A login attempt is bound to the browser that started it. |
| `EINV-11a` | Durability before acknowledgement: a 2xx is never returned before the commit succeeds. |
| `EINV-12` | Terminal domain states are terminal. |
| `EINV-13` | Every calculation context consumes current facts only. |
| `EINV-14` | A persisted derived linkage agrees with the fact it derives from. |
| `EINV-15` | A status assertion corresponds to the facts it was made about. |
| `EINV-16` | Unexpected-failure logging never emits raw payloads or bound values. |
| `EINV-17` | Day-level allocation preserves total elapsed time. |
| `EINV-18` | One authoritative version per submission scope. |
| `EINV-19` | Fact, legal treatment, account credit and remuneration basis are separate layers. |
| `EINV-20` | Policy editions are never back-dated and never re-pointed. |
| `EINV-21` | Calendar editions are insert-only. |
| `EINV-22` | Day-status facts are employee-owned, versioned facts. |
| `EINV-23` | Authority is a property of the writer, not of the request body. |
| `EINV-24` | A submission gate is decided on the same facts the frozen artefact records. |
| `EINV-25` | A derived metric is not a calculation input, and not a policy verdict. |
| `EINV-26` | A version chain never crosses employees. |
| `EINV-27` | A denormalised plan cache always agrees with the revision it caches. |
| `EINV-28` | Plan writability is a property of the writer, denied by default. |
| `EINV-29` | A plan is mutable only while nothing depends on it. |
| `EINV-30` | Retracting a mistaken plan and cancelling a planned shift are two different acts. |
| `EINV-31` | A stale identity is never silently redirected. |
| `EINV-32` | The two plan domains have two active predicates, over two different enums. |
| `EINV-33` | A factual disclosure is separated from an entitlement determination. |
| `EINV-34` | A create command's idempotency key is globally unique, and a collision is recovered rather than raised. |
| `EINV-35` | An unrecognised provenance is never presented as an approval. |
| `EINV-36` | An un-lived day is never charged, and never carried. |

## Why the invariants are separate from the rules

A rule (`WT-CORE-006`) says what the law requires. An invariant (`EINV-09`) says how the system
must behave for the rule's answer to remain trustworthy later. Keeping them apart is what lets a
policy edition change without weakening the integrity guarantees underneath it. See
[ARCHITECTURE.md](ARCHITECTURE.md) and [POLICY_CONFIGURATION.md](POLICY_CONFIGURATION.md).