## 2026-07-23 - Fast Table Assignment Bounds Check
**Learning:** In `_table_assignment.py`, calculating bounds via multiple loops inside the DFS of `allocation_candidates` was a bottleneck. Min/max operations and dict comprehensions overhead were prominent in the profiler.
**Action:** Inlined bounds calculations into a single loop, replacing `min`/`max` with explicit conditionals, avoiding dict comprehension overhead, and minimizing repeated lookup.
