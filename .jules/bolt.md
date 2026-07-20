## 2024-07-17 - O(N^2) Nested List Comprehension Anti-Pattern
**Learning:** Found an `O(N^2)` list comprehension pattern in `src/synthdb/graph/dependency.py` (`phase_layers`) where we were re-iterating over the entire graph `condensation` to group nodes by layer. This can become a severe bottleneck for large schemas (many tables/SCCs).
**Action:** Replace nested loops that filter over the same collection `O(N)` times with a single-pass `O(N)` grouping approach (e.g. accumulating into a pre-allocated array of arrays or hash map).
