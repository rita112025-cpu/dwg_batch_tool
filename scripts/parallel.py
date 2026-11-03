"""Run batch jobs on several accoreconsole processes at once.

Each worker thread drives its own accoreconsole over a slice of the jobs, so
per-process startup is still paid once per slice, not once per file.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Sequence, TypeVar

T = TypeVar("T")
R = TypeVar("R")

MAX_WORKERS = 8


def default_workers() -> int:
    """Half the logical CPUs, between 1 and 4: accoreconsole is memory hungry."""
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def clamp_workers(value) -> int:
    """Coerce a config / UI value to ``1..MAX_WORKERS``; bad input means 1."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(MAX_WORKERS, number))


def split(items: Sequence[T], workers: int) -> List[List[T]]:
    """Round-robin ``items`` into at most ``workers`` non-empty groups.

    Round-robin (not contiguous blocks) spreads big drawings that sit next to
    each other in a folder across the workers.
    """
    count = max(1, min(clamp_workers(workers), len(items)))
    return [list(items[index::count]) for index in range(count) if items[index::count]]


def run_groups(groups: Sequence[Sequence[T]], fn: Callable[[Sequence[T]], R]) -> List[R]:
    """Call ``fn(group)`` for every group, in parallel when there is more than one.

    Results keep the order of ``groups``. The first exception from any worker is
    re-raised after all workers have finished.
    """
    if len(groups) <= 1:
        return [fn(group) for group in groups]
    with ThreadPoolExecutor(max_workers=len(groups), thread_name_prefix="accore") as pool:
        futures = [pool.submit(fn, group) for group in groups]
        errors = [future.exception() for future in futures]
    for error in errors:
        if error is not None:
            raise error
    return [future.result() for future in futures]
