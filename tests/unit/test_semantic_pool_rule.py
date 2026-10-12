"""candidate_pool: count the scope first, then rank it exactly or ask the index."""

from __future__ import annotations

import uuid

import pytest

from mail_verdict.embeddings.search import Pool, _Candidate, candidate_pool

K = 4
MAX_ROWS = 10


class Steps:
    """The four database steps as recording stubs."""

    def __init__(self, *, inside: int, survivors: list[int]) -> None:
        self.inside = inside
        self.survivors = list(survivors)  # eligible count per eligible() call
        self.calls: list[str] = []

    async def count(self) -> int:
        self.calls.append("count")
        return self.inside

    async def exact(self) -> Pool:
        self.calls.append("exact")
        return [(uuid.uuid4(), 1.0 - i / 100) for i in range(min(K, self.inside))]

    async def index(self, n: int) -> list[_Candidate]:
        self.calls.append(f"index({n})")
        return [
            _Candidate(uuid.uuid4(), f"k{i}", uuid.uuid4(), 1.0 - i / 100) for i in range(n)
        ]

    async def eligible(self, candidates: list[_Candidate]) -> Pool:
        self.calls.append("eligible")
        # Scrambled on purpose: the index may return rows out of order.
        keep = candidates[: self.survivors.pop(0)]
        return [(c.account_id, c.similarity) for c in reversed(keep)]


async def run(steps: Steps) -> Pool:
    return await candidate_pool(
        count=steps.count, exact=steps.exact, index=steps.index, eligible=steps.eligible,
        k=K, exact_scan_max_rows=MAX_ROWS,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inside", "survivors", "calls", "size"),
    [
        (0, [], ["count"], 0),
        (1, [], ["count", "exact"], 1),
        (MAX_ROWS, [], ["count", "exact"], K),
        (MAX_ROWS + 1, [K], ["count", f"index({K})", "eligible"], K),
        (
            MAX_ROWS + 1, [K - 1, K],
            ["count", f"index({K})", "eligible", f"index({K * 4})", "eligible"], K,
        ),
        (
            MAX_ROWS + 1, [K - 1, K - 1],
            ["count", f"index({K})", "eligible", f"index({K * 4})", "eligible"], K - 1,
        ),
        (
            MAX_ROWS + 1, [0, 0],
            ["count", f"index({K})", "eligible", f"index({K * 4})", "eligible"], 0,
        ),
    ],
)
async def test_the_pool_rule(
    inside: int, survivors: list[int], calls: list[str], size: int,
) -> None:
    steps = Steps(inside=inside, survivors=survivors)

    pool = await run(steps)

    assert steps.calls == calls
    assert len(pool) == size


@pytest.mark.asyncio
async def test_the_pool_comes_back_nearest_first_whatever_order_the_index_used() -> None:
    steps = Steps(inside=MAX_ROWS + 1, survivors=[K])

    pool = await run(steps)

    scores = [similarity for _, similarity in pool]
    assert scores == sorted(scores, reverse=True)
