# Policy configuration mechanics

Policy values that can affect a calculation are typed, immutable, code-owned records. The current
records are `OrdinaryTimeParameters` and `TravelParameters`, wrapped by `PolicyEdition` in
[`backend/app/policy/editions.py`](../backend/app/policy/editions.py). `EDITIONS` maps each
`engine_rule_set` identity to its frozen parameter set; `HISTORICAL_RULE_SETS` preserves the
append-only set of identities that have shipped. Shift, OCD and overtime editions currently carry
identity only (`parameters=None`); their numeric logic remains in their domain code.

The database `policy_version` row owns the edition identity and provenance: policy key, version
label, `engine_rule_set`, source-provenance hash, lifecycle status, and inclusive `effective_from` /
`effective_to` dates. It does not own a mutable copy of parameter values. The persistence adapter
resolves the row for the month being calculated by selecting the covering date range, then resolves
the matching code edition and supplies its typed parameters to the calculation. Ordinary resolution
is in [`backend/app/policy/service.py`](../backend/app/policy/service.py); travel resolution is in
that module's `travel_edition_for`. A missing row, unknown rule set, or missing expected typed
parameter fails closed; resolution does not fall back to the newest edition. Results retain the
selected policy-version identity in their calculation snapshot.

The policy's own effective date and the database resolution interval answer different questions.
The policy date records when a rule applies; the row's month range chooses the frozen code edition
for a calculation period. The seeded v1 rows resolve from `2026-06-01`. Their database lifecycle
status `PROVISIONAL` is not the normative implementation status of any individual rule and does not
mean the rule is enabled. Rule status is documented separately in the [rule
catalogue](RULE_CATALOGUE.md); registration and actual activation are separate again.

## What stays in code

Elapsed-time and DST handling, interval allocation, overlap and break subtraction, rest grouping,
allowance classification, OCD/shift algorithms, authorization and disclosure checks, correction
lineage, and snapshot reproduction stay as code behavior. Effective-dated contract, FTE, schedule,
calendar and status facts remain in their existing data models and publication workflows. Some
thresholds are typed; some remain algorithm constants. In particular, the 30/45-minute qualifying
break totals and several finding-message thresholds are still code literals. A typed value is not a
promise that every neighboring message or algorithm can be tuned independently. See
[`RULE_CATALOGUE.md`](RULE_CATALOGUE.md) for each rule's parameters and code location.

## Changing a value

A behavior-changing value is introduced through a new registered edition with a new identity and
resolution boundary. The new `EDITIONS` entry and its database publication migration land together;
an existing edition or its historical parameter set is never edited or re-pointed. No month boundary,
policy effective date, or unresolved payout month is inferred from a default. The selected parameter
set remains in code and calculation snapshots continue to point to the database edition identity.

An unpublished proposal can be represented as a typed code value without adding it to `EDITIONS`,
`HISTORICAL_RULE_SETS`, or `policy_version`. For example, the corrected travel `TOTAL` base is
expressible as a code-owned parameter value, but the v2 edition is not registered or published.
The existing row continues to resolve to the shipped `EXCESS` behavior until an authorized boundary
is established. A blocked or unresolved value is represented as absent/unavailable, or as an
explicitly unregistered proposal; it is never represented by a guessed numeric default or a
placeholder edition. Open owner decisions stay recorded as explicit gaps in the rule catalogue.

Rule parameters are versioned code, not a runtime rule engine. There is no runtime JSON/YAML loader,
policy admin UI, rule interpreter, or second mutable parameter registry.