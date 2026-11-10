"""Transition (legacy-compatible) report renderers.

Presentation-only CSV renderers over values the deterministic engine already
computed or over frozen submission payloads (BE spec §14: templates never
recalculate). Semantic compatibility for the shadow pilot — not Excel styling,
not the workbook formula graph (INV-12, INV-13). No recipient-side export:
delivery stays employee-mediated like today's workbook email (docs/SECURITY.md).
"""
