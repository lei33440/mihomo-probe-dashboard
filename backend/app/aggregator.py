"""聚合与清理任务。"""
from __future__ import annotations

import logging
import time

from . import repository as repo
from .config import settings

log = logging.getLogger(__name__)


def run_aggregations() -> None:
    now_ms = int(time.time() * 1000)

    if settings.agg_5min_enabled:
        try:
            n = repo.aggregate_into("aggregates_5min", 300)
            if n:
                log.info("5min aggregates inserted: %s", n)
        except Exception:
            log.exception("aggregate 5min failed")

    if settings.agg_1h_enabled:
        try:
            n = repo.aggregate_into("aggregates_1h", 3600)
            if n:
                log.info("1h aggregates inserted: %s", n)
        except Exception:
            log.exception("aggregate 1h failed")

    raw_cutoff = now_ms - settings.retention_raw_days * 86400_000
    try:
        n = repo.prune_old("probes", raw_cutoff)
        if n:
            log.info("pruned raw probes: %s", n)
    except Exception:
        log.exception("prune probes failed")

    five_min_cutoff = now_ms - settings.retention_5min_days * 86400_000
    try:
        n = repo.prune_old("aggregates_5min", five_min_cutoff)
        if n:
            log.info("pruned 5min aggregates: %s", n)
    except Exception:
        log.exception("prune 5min failed")