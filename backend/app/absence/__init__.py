"""Day-status facts (BE-ENUM-002, narrowed): the canonical absence/day-status layer.

The module owns the canonical, employee-owned, versioned day-status fact today
and a mapped-external adapter later. The external HR system is workflow and
vocabulary, never the working-time ledger (INV-01); only source-backed
working-time effects enter the calculation domain.
"""
