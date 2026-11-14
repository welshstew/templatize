"""Guard against quadratic slow-downs: they have crept in twice (overlap checks in
merging, then part lookups for composite values). Linear code does this in ~1.5 s;
the quadratic versions took over 20 s. The limit is generous for slow machines."""

from __future__ import annotations

import time

from templatize.suggest import suggest

LIMIT_S = 10.0


def test_suggest_scales_on_ten_thousand_list_items() -> None:
    text = "users:\n" + "".join(
        f"  - name: user{i}\n    email: u{i}@x.example\n    role: {'admin' if i % 3 else 'dev'}\n"
        for i in range(10_000)
    )
    t0 = time.perf_counter()
    res = suggest(text)
    elapsed = time.perf_counter() - t0
    assert len(res.loops) == 1 and len(res.loops[0].items) == 10_000
    assert elapsed < LIMIT_S, f"suggest took {elapsed:.1f}s on 10k list items"
