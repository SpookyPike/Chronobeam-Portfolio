# Policy sources and provenance

Chronobeam executes working-time rules, so every executable rule needs a stated basis. This
register is that basis, and it is deliberately narrow: **two** classes only.

| Class | Meaning | Binding? |
|---|---|---|
| `LAW-*` | A public statute or directive, cited to an authoritative published source. | Yes, where it applies. |
| `POL-*` | A sample policy shipped with this demonstration build. | **No.** Configuration, not law. |

There is no third class. Nothing in this repository derives from a confidential agreement,
an unpublished works agreement, an internal guideline, or an employer-specific document.

---

## 1. Statutory sources (`LAW-*`)

| ID | Instrument | Provisions relied on | Source |
|---|---|---|---|
| `LAW-01` | Arbeitszeitgesetz (ArbZG) | §2 definitions (working time excludes breaks; night time 23:00–06:00; night work = more than two hours of night time; night worker = 48+ nights per calendar year); §3 eight hours, extendable to ten if the average stays at eight over six calendar months or 24 weeks; §4 breaks — 30 minutes above six hours, 45 minutes above nine hours, in segments of at least 15 minutes, no more than six hours without a break; §5 eleven hours of uninterrupted rest; §6 night and shift work; §9 Sunday and public-holiday rest; §11 fifteen work-free Sundays and a replacement rest day within two weeks (Sunday) or eight weeks (weekday holiday); §16 record keeping, two years | [gesetze-im-internet.de/arbzg](https://www.gesetze-im-internet.de/arbzg/) |
| `LAW-02` | Bundesurlaubsgesetz (BUrlG) | §3 statutory leave; §7 timing, transfer to the next calendar year and the three-month grant window | [gesetze-im-internet.de/burlg](https://www.gesetze-im-internet.de/burlg/) |
| `LAW-03` | Bundesdatenschutzgesetz (BDSG) | §26 processing of employee data for the employment relationship | [gesetze-im-internet.de/bdsg_2018/__26](https://www.gesetze-im-internet.de/bdsg_2018/__26.html) |
| `LAW-04` | GDPR / DSGVO | Art. 5 (principles), 6 (lawfulness), 15 (access), 17 (erasure), 32 (security) | [eur-lex.europa.eu/eli/reg/2016/679/oj](https://eur-lex.europa.eu/eli/reg/2016/679/oj) |
| `LAW-05` | Directive 2003/88/EC | Art. 3 daily rest, 4 breaks, 5 weekly rest, 6 maximum weekly working time, 8 night work | [eur-lex.europa.eu/eli/dir/2003/88/oj](https://eur-lex.europa.eu/eli/dir/2003/88/oj) |
| `LAW-06` | Gesetz über Sonn- und Feiertage des Landes Schleswig-Holstein (SFTG) | §2(1) — the ten statutory public holidays the calendar module derives | [§2 SFTG](https://www.gesetze-rechtsprechung.sh.juris.de/bssh/document/jlr-FeiertGSH2004pP2) |

`LAW-06` is a state statute, not a federal one, because public holidays in Germany are Land
competence. The demonstration build derives the Schleswig-Holstein set; a deployment elsewhere
supplies its own statutory day set through the ordinary calendar-publication path and changes no
code.

## 2. Demonstration policy (`POL-*`)

Each `POL-*` entry is a **sample rule set with sample parameters**. The numbers are chosen to
make the product demonstrable, not to state an entitlement. A real deployment replaces them with
its own negotiated terms; the code treats them as versioned, effective-dated editions, so a
replacement is a new edition rather than an edit to history.

| ID | Covers | Nature |
|---|---|---|
| `POL-01` | Ordinary working time and the flex account: the 06:00–21:00 trusted-flex corridor, the 50 % / 100 % balance warning thresholds, the two-day negative-balance warning, the year-end settlement boundary and payout cap, compensation-day limits (20 per year, at most five consecutive) | Sample policy |
| `POL-02` | Shift work: plan-versus-actual separation, paid shift breaks, at most seven shift days in two weeks (nine if voluntary), at most twelve consecutive working days, the late-night definition, Sunday/holiday compensation-day timing, cancellation notice at 14 and 3 days, overrun bands at 7:48 h and 10 h | Sample policy |
| `POL-03` | On-call duty (OCD): availability versus call-out work, schedule notice, the monthly assignment cap, daily and weekly limits applied to effective call-out work, the 11-hour rest and next-day credit, travel on an on-site call, availability remuneration | Sample policy |
| `POL-04` | Allowance and supplement schedule: overtime base compensation, pay-group buckets, night, Saturday, Sunday, public-holiday and `Mehrarbeit` supplements, overlap handling, and the month-scoped election | Sample policy |
| `POL-05` | Absence and day-status workflow: approved vacation, compensation days, sickness, partial-day sickness, idempotent external events, and reconciliation against recorded work | Sample policy |
| `POL-06` | Remote work: work-time rules apply remotely, no behaviour or performance monitoring, and the planning/approval workflow that the ledger deliberately does not enforce | Sample policy |
| `POL-07` | Vacation workflow: half-day vacation, carry-over handling, and the interface to the external HR absence system | Sample policy |
| `POL-08` | Travel-time credit: business-trip status is not working time, active work and driving count, passive travel, the 15-hour monthly non-chargeable threshold and the 25 % credit, and the day-off rule after an intercontinental return | Sample policy |
| `POL-09` | Overtime authorisation: prior authorisation, the four-week retroactive exception, and who may author an authoritative approval | Sample policy |
| `POL-10` | Privacy and disclosure: the employee owns ledger visibility, HR and supervisors have no standing access, a random-check request reveals no data, and every grant is recipient-, purpose- and scope-bound | **Product policy.** A design commitment, not a statutory requirement |

`POL-10` is listed here because the code enforces it as hard as any rule, but it is an
architectural and ethical commitment of this product rather than a legal provision. Statutory
data-protection law (`LAW-03`, `LAW-04`) sets a floor; `POL-10` goes further on purpose.

## 3. Provenance digest

`policy_version.source_manifest_hash` records **which generation of this register** an executable
edition was published against. The value is the SHA-256 of this file:

```bash
sha256sum docs/POLICY_SOURCES.md
```

The digest is provenance, not a control. A changed register means a new register generation, and
an edition that must change with it is a **new edition with a new effective date** — never an
edit to one that has already accounted for a month. `HISTORICAL_RULE_SETS` in
`backend/app/policy/editions.py` is append-only for the same reason.

## 4. What is deliberately absent

- No employer-specific agreement, works agreement, internal guideline, FAQ or manual is
  reproduced, paraphrased or cited.
- No confidential source identifier, file hash or passage appears anywhere in this repository.
- No claim is made that a `POL-*` value is legally required, customary, or negotiated.
- No legal advice is offered. Where `LAW-*` and a `POL-*` rule interact, the statute wins and the
  code says so at the rule.

Rule-by-rule mapping: [RULE_CATALOGUE.md](RULE_CATALOGUE.md). How editions and parameters are
versioned: [POLICY_CONFIGURATION.md](POLICY_CONFIGURATION.md).