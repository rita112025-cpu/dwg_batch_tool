import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import parallel  # noqa: E402


def test_clamp_workers():
    assert parallel.clamp_workers("3") == 3
    assert parallel.clamp_workers(0) == 1
    assert parallel.clamp_workers(None) == 1
    assert parallel.clamp_workers(99) == parallel.MAX_WORKERS
    assert 1 <= parallel.default_workers() <= 4


def test_split_round_robin():
    assert parallel.split([1, 2, 3, 4, 5], 2) == [[1, 3, 5], [2, 4]]
    assert parallel.split([1, 2], 4) == [[1], [2]]
    assert parallel.split([], 3) == []


def test_run_groups_runs_concurrently_and_keeps_order():
    barrier = threading.Barrier(3, timeout=5)

    def work(group):
        barrier.wait()
        return sum(group)

    assert parallel.run_groups([[1], [2, 3], [4]], work) == [1, 5, 4]


def test_run_groups_reraises_worker_error():
    def work(group):
        if group == [2]:
            raise ValueError("boom")
        return group

    with pytest.raises(ValueError):
        parallel.run_groups([[1], [2]], work)
