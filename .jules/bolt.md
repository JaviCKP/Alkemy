## 2026-07-22 - [Performance]
**Learning:** `min()` and `max()` are slow in tight Python loops. Using `if` conditions with local variables provides a massive performance boost (reduced a 57s test by ~17s, a ~30% improvement) in `allocation_candidates` in `_table_assignment.py`, which is heavily called.
**Action:** Always prefer `if` conditional checks over `min()` and `max()` for bounds-checking operations executed millions of times in Python hot loops.
