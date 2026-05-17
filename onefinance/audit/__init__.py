"""Audit log — structured tracing for every API invocation.

Records provider calls, cache hits, tier-walking decisions, and
errors so you can answer "which provider served this?" and
"how many FMP calls did I burn today?"

Usage::

    from onefinance.audit import AuditLog, AuditEntry, AuditStats

    log = AuditLog()
    entries = log.query(provider="fmp", limit=10)
    stats = log.stats()
"""

from onefinance.audit.log import AuditLog
from onefinance.audit.models import AuditEntry, AuditStats

__all__ = [
    "AuditEntry",
    "AuditLog",
    "AuditStats",
]
