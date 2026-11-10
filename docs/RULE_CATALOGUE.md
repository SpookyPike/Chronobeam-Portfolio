# Rule catalogue

Every executable rule in Chronobeam has a stable ID and a stated basis. The basis is either a
public statute (`LAW-*`) or a sample policy shipped with this demonstration build (`POL-*`).
Both registers are defined in [POLICY_SOURCES.md](POLICY_SOURCES.md).

This catalogue is an index. It is maintained by hand, reviewed with the code, and guarded by a
test in one direction only: every rule ID that appears in the code must have a row here, and every
row's status must agree with its basis. The reverse direction is not enforced, because a row may
document a rule that is deliberately `BLOCKED` and has no executable counterpart.

## Status vocabulary

| Status | Meaning |
|---|---|
| `STATUTORY` | Implemented, and the implemented behaviour is the statutory behaviour. |
| `POLICY` | Implemented from a sample policy. The numbers are configuration, not law. |
| `PRODUCT` | Implemented product/privacy commitment. Not a legal requirement. |
| `PARTIAL` | Part of the rule is enforced; the rest is deliberately left to the operator. |
| `BLOCKED` | Facts are captured, but the rule refuses to produce a verdict. |

`BLOCKED` is a design position, not an omission. Where a rule needs a parameter this
demonstration build has no defensible value for, the code records the fact, reports it as an
open item, and does not invent a threshold. A guessed threshold presented as authority would be
worse than a visible gap.

## Ordinary working time

| Rule | Title | Basis | Status |
|---|---|---|---|
| `WT-CORE-001` | Actual work is factual interval data | `LAW-01` §2 (working time excludes breaks) | `STATUTORY` |
| `WT-CORE-002` | No interval overlap | Integrity constraint, `EINV-05` (not a statutory rule) | `PRODUCT` |
| `WT-CORE-003` | Qualifying break after more than six hours | `LAW-01` §4 sentence 1 (30 minutes) | `STATUTORY` |
| `WT-CORE-004` | Qualifying break after more than nine hours | `LAW-01` §4 sentence 1 (45 minutes) | `STATUTORY` |
| `WT-CORE-005` | No block longer than six hours without a break | `LAW-01` §4 sentence 3 | `STATUTORY` |
| `WT-CORE-006` | Ten-hour daily ceiling (the §3 averaging condition is not computed) | `LAW-01` §3 | `PARTIAL` |
| `WT-CORE-007` | Eleven-hour daily rest | `LAW-01` §5(1) | `STATUTORY` |
| `WT-CORE-008` | Trusted-flex corridor 06:00–21:00 | `POL-01` | `POLICY` |
| `WT-CORE-009` | Overtime authorised in advance, four-week retroactive exception | `POL-09` | `PARTIAL` |

`WT-CORE-003` and `WT-CORE-004` count only pauses of at least 15 minutes, which is
`LAW-01` §4 sentence 2. The engine removes explicit breaks before measuring, so a paid break still
interrupts the statutory block even though the ledger credits it.

## Flex account

| Rule | Title | Basis | Status |
|---|---|---|---|
| `WT-FLEX-001` | Effective-dated weekday required time | `POL-01` | `POLICY` |
| `WT-FLEX-002` | Daily flex delta | `POL-01` | `POLICY` |
| `WT-FLEX-003` | Warning at more than 50 % of weekly hours | `POL-01` | `POLICY` |
| `WT-FLEX-004` | Critical at more than 100 % of weekly hours | `POL-01` | `POLICY` |
| `WT-FLEX-005` | Negative-balance two-day threshold | `POL-01` | `POLICY` |
| `WT-FLEX-006` | Year-end positive balance and payout cap | `POL-01` | `POLICY` |
| `WT-FLEX-007` | Year-end negative balance exact settlement | `POL-01` | `BLOCKED` |
| `WT-FLEX-008` | Compensation-day limits (20 per year, 5 consecutive) | `POL-01` | `POLICY` |
| `WT-FLEX-009` | Paid time does not credit the working-time account | `POL-01` | `BLOCKED` |

## Absence and day status

| Rule | Title | Basis | Status |
|---|---|---|---|
| `ABSENCE-001` | The external HR system owns absence status, not working time | `POL-05` | `POLICY` |
| `ABSENCE-002` | Approved vacation reduces required time | `POL-05`, `LAW-02` §3 | `POLICY` |
| `ABSENCE-003` | Approved compensation days | `POL-05` | `POLICY` |
| `ABSENCE-004` | Sickness import is minimal | `POL-05`, `LAW-03` §26, `LAW-04` Art. 5 | `POLICY` |
| `ABSENCE-005` | Remote-work status is plan/default only | `POL-06` | `POLICY` |
| `ABSENCE-006` | External event idempotency | `POL-05` | `POLICY` |
| `ABSENCE-007` | Imported status never overwrites recorded work | `POL-05` | `POLICY` |
| `ABSENCE-008` | Partial-day sickness treatment | `POL-05` | `BLOCKED` |
| `WT-STATUS-001` | Work recorded on a full vacation day | `POL-05` | `POLICY` |
| `WT-STATUS-002` | Work recorded on a sickness day | `POL-05` | `POLICY` |
| `WT-STATUS-003` | Work recorded on a compensation day | `POL-05` | `POLICY` |
| `WT-STATUS-004` | Day-status coverage exceeds one day | `POL-05` | `POLICY` |
| `WT-STATUS-005` | Day status on a day with no scheduled requirement | `POL-05` | `POLICY` |

## Shift work

| Rule | Title | Basis | Status |
|---|---|---|---|
| `SHIFT-001` | Plan and actual are distinct objects | `POL-02` | `POLICY` |
| `SHIFT-002` | Exact-as-planned needs no deviation record | `POL-02` | `POLICY` |
| `SHIFT-003` | Paid breaks during a shift must be recorded as explicit facts | `POL-02` | `POLICY` |
| `SHIFT-004` | At most seven shift days in two weeks, nine if voluntary | `POL-02` | `BLOCKED` |
| `SHIFT-005` | At most twelve consecutive working days | `POL-02` | `BLOCKED` |
| `SHIFT-006` | Late-night shift definition | `LAW-01` §2(3); `POL-02` | `BLOCKED` |
| `SHIFT-007` | Late-night sequence and rest | `LAW-01` §5; `POL-02` | `BLOCKED` |
| `SHIFT-008` | Sunday and public-holiday replacement-day windows (no executable check; the operator owns the roster) | `LAW-01` §11(3) | `BLOCKED` |
| `SHIFT-009` | Cancellation more than fourteen days ahead | `POL-02` | `POLICY` |
| `SHIFT-010` | Cancellation less than three days ahead | `POL-02` | `BLOCKED` |
| `SHIFT-011` | Shift overrun 7:48 h to 10 h | `POL-02` | `POLICY` |
| `SHIFT-012` | Shift presence longer than ten hours (elapsed start to end, breaks included; `WT-CORE-006` is the break-free statutory check) | `POL-02` | `POLICY` |
| `SHIFT-013` | Decision authority after a policy boundary | `POL-02` | `BLOCKED` |

## On-call duty (OCD)

| Rule | Title | Basis | Status |
|---|---|---|---|
| `OCD-001` | Availability and call-out work are separate objects | `POL-03` | `POLICY` |
| `OCD-002` | Schedule notice period | `POL-03` | `POLICY` |
| `OCD-003` | Monthly assignment cap | `POL-03` | `POLICY` |
| `OCD-004` | Daily and weekly limits apply to effective call-out work | `LAW-01` §3, §6(2); `POL-03` | `PARTIAL` |
| `OCD-005` | Eleven-hour rest and next-day credit | `LAW-01` §5(1); `POL-03` | `PARTIAL` |
| `OCD-006` | Travel on an on-site call | `POL-03` | `POLICY` |
| `OCD-007` | Availability remuneration | `POL-03` | `POLICY` |

`OCD-004` and `OCD-005` are `PARTIAL` because the rest and limit checks run on the recorded
call-out intervals, while the credit for a call that runs past the next day's start is reported
rather than silently netted into the balance.

## Allowances and supplements

| Rule | Title | Basis | Status |
|---|---|---|---|
| `ALLOW-001` | Overtime base compensation | `POL-04` | `POLICY` |
| `ALLOW-002` | Overtime supplement by pay-group bucket | `POL-04` | `POLICY` |
| `ALLOW-003` | Night supplement | `POL-04` | `POLICY` |
| `ALLOW-004` | Saturday supplement | `POL-04` | `POLICY` |
| `ALLOW-005` | Sunday supplement | `POL-04` | `POLICY` |
| `ALLOW-006` | Public-holiday supplement | `POL-04` | `POLICY` |
| `ALLOW-007` | `Mehrarbeit` supplement | `POL-04` | `POLICY` |
| `ALLOW-008` | Overlapping Saturday, Sunday and holiday | `POL-04` | `POLICY` |
| `ALLOW-009` | Month-scoped, splittable overtime remuneration election | `POL-04` | `PARTIAL` |

Every percentage here is sample configuration, and so is the window it applies to: the
supplement window is 21:00 to 06:00 (`POL-04`), which is wider than the statutory night window
of `LAW-01` §2(3), 23:00 to 06:00. No statutory night-work classifier is implemented; the
supplement window is not a claim about the statute.

## Remote work

| Rule | Title | Basis | Status |
|---|---|---|---|
| `REMOTE-001` | Working-time rules apply remotely | `LAW-01` §2; `POL-06` | `STATUTORY` |
| `REMOTE-002` | No behaviour or performance monitoring | `POL-06`, `LAW-03` §26, `LAW-04` Art. 5 | `PRODUCT` |
| `REMOTE-003` | Planning and approval workflow is not enforced by the ledger | `POL-06` | `POLICY` |
| `REMOTE-004` | Remote work abroad | `POL-06` | `POLICY` |

## Vacation

| Rule | Title | Basis | Status |
|---|---|---|---|
| `VAC-001` | The external HR system owns the vacation workflow | `POL-07` | `POLICY` |
| `VAC-002` | Half-day vacation | `POL-07` | `POLICY` |
| `VAC-003` | Carry-over deadline (no executable check; the external HR system owns the vacation workflow) | `LAW-02` §7(3); `POL-07` | `BLOCKED` |

## Travel

| Rule | Title | Basis | Status |
|---|---|---|---|
| `TRAVEL-001` | Business-trip status is not working time | `POL-08` | `POLICY` |
| `TRAVEL-002` | Active work and driving count as working time | `POL-08` | `POLICY` |
| `TRAVEL-003` | Passive travel does not create a deficit by itself | `POL-08` | `POLICY` |
| `TRAVEL-004` | More than 15 h non-credited travel earns a 25 % credit | `POL-08` | `PARTIAL` |
| `TRAVEL-005` | Day off after an intercontinental return | `POL-08` | `BLOCKED` |
| `TRAVEL-SUNDAY-001` | Sunday travel work approval advisory | `LAW-01` §9, §11; `POL-08` | `POLICY` |
| `TRAVEL-SUNDAY-002` | Sunday driving advisory | `LAW-01` §9, §11; `POL-08` | `POLICY` |
| `TRAVEL-LOCAL-001` | Foreign local-law assessment boundary | `POL-08` | `POLICY` |

`TRAVEL-LOCAL-001` is an explicit refusal: the engine does not model foreign working-time law,
and says so instead of applying the German thresholds abroad.

## Privacy and disclosure

| Rule | Title | Basis | Status |
|---|---|---|---|
| `ACCESS-001` | The employee owns default ledger visibility | `POL-10` | `PRODUCT` |
| `ACCESS-002` | Central storage does not imply HR browse access | `POL-10` | `PRODUCT` |
| `ACCESS-003` | Findings are employee-private by default | `POL-10` | `PRODUCT` |
| `ACCESS-004` | Shift report disclosure is a projection | `POL-10` | `PRODUCT` |
| `ACCESS-005` | OCD compensation disclosure is a projection | `POL-10` | `PRODUCT` |
| `ACCESS-006` | Overtime disclosure is a projection | `POL-10` | `PRODUCT` |
| `ACCESS-007` | A random-check request reveals no data | `POL-10` | `PRODUCT` |
| `ACCESS-008` | The employee grants the random-check view | `POL-10` | `PRODUCT` |
| `ACCESS-009` | A grant is scope-bound | `POL-10` | `PRODUCT` |
| `ACCESS-010` | Correction is allowed before disclosure | `POL-10` | `PRODUCT` |
| `ACCESS-011` | Correction after disclosure keeps lineage | `POL-10` | `PRODUCT` |
| `ACCESS-012` | Supervisor access is not implicit | `POL-10` | `PRODUCT` |
| `ACCESS-013` | Technical administration is not business-data readership | `POL-10` | `PRODUCT` |
| `ACCESS-014` | Purpose-specific retention is separate from canonical retention | `POL-10`, `LAW-04` Art. 5 | `PARTIAL` |
| `ACCESS-015` | A disclosure grant is authorisation, not consent | `POL-10`, `LAW-04` Art. 6 | `PRODUCT` |
| `ACCESS-016` | Sensitive absence data is minimised | `POL-10`, `LAW-03` §26 | `PRODUCT` |
| `ACCESS-017` | Only an authorised writer may author an authoritative approval | `POL-10` | `PRODUCT` |

## What the statutory checks actually emit

The five `LAW-01` findings an employee can see, with the exact condition the engine applies:

| Finding | Condition |
|---|---|
| `WT-CORE-003` | more than 6 h worked and qualifying break < 30 min |
| `WT-CORE-004` | more than 9 h worked and qualifying break < 45 min |
| `WT-CORE-005` | more than 6 h worked in one block without a qualifying break |
| `WT-CORE-006` | more than 10 h worked in a day |
| `WT-CORE-007` | less than 11 h between the end of one working period and the start of the next |

These are warnings for the employee. They are not sent to HR, and `POL-10` is why: a compliance
finding exists to help the person it describes.