"""Clean-cutover opening state (BE spec §5.8, CUTOVER-T01/T02).

One-time controlled ``CUTOVER_SEED`` ledger entries — the ONLY production
state imported from the legacy Excel process (validated closing flex balance
/ authorized outstanding credits). Historical workbooks are never imported
(CUTOVER-T03, INV-13); they remain archive evidence outside this repository.
"""
