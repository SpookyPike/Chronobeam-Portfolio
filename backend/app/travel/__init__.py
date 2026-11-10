"""Employee-declared business-trip facts and deterministic travel calculations.

Generic worktime endpoints must reject ``BUSINESS_TRIP_WORK`` / travel-linked
intervals.  Router registration and that protective seam are owned by the
Phase-9 integration slice, not this bounded domain module.
"""
