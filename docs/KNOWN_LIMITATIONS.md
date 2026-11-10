# Known limitations

This page is the honest boundary of what this repository proves. Everything here is deliberate:
either the fact is captured and the verdict withheld, or the capability is an adapter boundary
with no live counterpart.

## Enterprise integrations are boundaries, not connections

| Boundary | State in this repository |
|---|---|
| HR absence system | Typed adapter contract and synthetic import events. No live connection. |
| Shift-plan source | Typed import/seed seam. The employee records their own planned shifts instead. |
| On-call (OCD) schedule | Same seam. Availability is employee-entered or seeded. |
| Payroll | Compensation is projected, never transmitted. |
| Master data | Effective-dated rows are maintained through the application. No upstream sync. |
| Identity provider | OIDC is implemented and exercised against a local test provider. No production IdP is configured or assumed. |

Each boundary is idempotent and fails closed. When an adapter is absent, the application stays
usable: the employee records the fact, and the fact is marked as declared rather than imported.

## Sample-policy parameters that are deliberately undecided

These rules capture facts and refuse to produce a verdict. A guessed threshold presented as
authority would be worse than a visible gap, so the code reports the gap instead.

| Rule | Why no verdict |
|---|---|
| `WT-FLEX-007` | The exact treatment of a negative balance at the year boundary is a negotiated question. |
| `WT-FLEX-009` | Whether paid overtime credits the flex account is a policy choice; v1 credits everything. |
| `ABSENCE-008` | How a half-day of sickness counts against required time is not settled. |
| `SHIFT-004`, `SHIFT-005` | Shift-day and consecutive-day limits depend on a shift policy that has open parameters. |
| `SHIFT-006`, `SHIFT-007` | The late-night definition and its rest consequence are parameterised but not fixed. |
| `SHIFT-010` | The short-notice cancellation consequence depends on an undecided notice rule. |
| `SHIFT-013` | Who may decide a cancellation after a policy boundary changes is open. |
| `TRAVEL-005` | The recovery day after an intercontinental return needs a rule this build does not fix. |
| `SHIFT-008` | The Sunday and public-holiday replacement-day windows are statutory, but no executable check exists: the operator owns the roster. |
| `VAC-003` | The carry-over deadline is statutory, but the external HR system owns the vacation workflow, so the ledger does not enforce it. |

`TRAVEL-004` is `PARTIAL`: the shipped edition credits 25 % of the amount **above** the monthly
threshold, while the sample policy describes 25 % of the **total**. Both forms are expressible; the
correction is designed, not published, so no already-computed month is restated.

`WT-CORE-006` is `PARTIAL` for a different reason. The daily ceiling is checked, but the statutory
extension to ten hours depends on the average staying at eight hours over six calendar months or
24 weeks, and that average is **not** computed. A month that breaches it is not detected.

The `OPEN-nn` identifiers in code comments name open questions carried over from the project's
own design register. That register is not published, so an `OPEN-nn` citation is a marker of a
provisional decision, not a checkable reference. The corresponding behaviour is listed in the
table above.

## Validation evidence

- The repository contains **no spreadsheet fixtures**. The predecessor project used a spreadsheet
  as a regression oracle; that fixture was confidential and is not published. Parity tooling is
  therefore not included, and no parity claim is made here.
- Browser coverage is a small set of critical journeys, not a full product sweep. Deterministic
  rule coverage lives in the backend unit and property tests, where it belongs.
- Backend integration tests require PostgreSQL and skip when it is absent. `CHRONOBEAM_REQUIRE_DB=1`
  turns that skip into a failure.

## Operational scope

- TLS termination, database transport protection, encryption at rest, secret storage, off-host
  backups, monitoring and rate limiting are deployment responsibilities. The application provides
  the seams and documents them; it does not provide the infrastructure.
- One application process plus one PostgreSQL database. Scheduled work is in-process. There is no
  external broker, cache or second datastore, and adding one is out of scope by design.
- Retention periods are deployment policy. No deletion period is hard-coded, and a purpose-specific
  retention choice is separate from the canonical ledger.
- Foreign working-time law is not modelled. `TRAVEL-LOCAL-001` states that boundary explicitly
  rather than applying German thresholds abroad.

## Not legal advice

The rule catalogue maps implemented behaviour to public statutes and to labelled sample policy.
It is an engineering document. It is not legal advice, and a deployment must confirm its own
obligations with its own advisers.