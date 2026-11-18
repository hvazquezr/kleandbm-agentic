# Baseline comparison: single candidate vs. selective ToT (northstar_structures)

Runs per variant: baseline 3, tot 3. Values are means over runs; rubric rows count the runs that passed. Rubric checks are keyword heuristics over the records (eval/baseline/grade.py), so check surprising rows in the run JSON.

| | baseline | tot |
|---|---|---|
| completed runs | 3/3 | 3/3 |
| **rubric score** (share of checks passed) | 1.0 | 1.0 |
| &nbsp;&nbsp;facts_declare_grain | 3/3 | 3/3 |
| &nbsp;&nbsp;guest_checkouts_handled | 3/3 | 3/3 |
| &nbsp;&nbsp;no_order_measures_on_lines | 3/3 | 3/3 |
| &nbsp;&nbsp;order_grain_fact_for_order_measures | 3/3 | 3/3 |
| &nbsp;&nbsp;order_value_feasible | 3/3 | 3/3 |
| &nbsp;&nbsp;refund_rate_feasible | 3/3 | 3/3 |
| &nbsp;&nbsp;returns_fact_separate | 3/3 | 3/3 |
| &nbsp;&nbsp;sales_by_both_stores_feasible | 3/3 | 3/3 |
| &nbsp;&nbsp;shipping_by_category_honest | 3/3 | 3/3 |
| &nbsp;&nbsp;store_roles_distinguished | 3/3 | 3/3 |
| Ready for Review | 0/3 | 0/3 |
| readiness blockers | 2.33 | 3.33 |
| Validator passed / failed / unverified | 10.67 / 0.0 / 2.67 | 12.33 / 0.0 / 3.0 |
| review rounds | 1.33 | 2.0 |
| tables | 7.0 | 8.0 |
| questions raised | 2.67 | 3.67 |
| decision points (total) | 0 | 3 |
| search outcomes | – | partial 2, selected 1 |
| seconds | 456.8 | 840.27 |
| model calls (all agents) | 37.0 | 46.33 |
| input tokens | 233931.67 | 434886.67 |
| output tokens | 40724.33 | 78957.0 |
| &nbsp;&nbsp;of which in searches | 0.0 | 29860.0 |
| cost (USD) | – (pass --prices) | – (pass --prices) |

## Runs

1. **baseline** · completed · draft · 376.2s · decision points: none · failed checks: none
2. **baseline** · completed · draft · 561.5s · decision points: none · failed checks: none
3. **baseline** · completed · draft · 432.7s · decision points: none · failed checks: none
4. **tot** · completed · draft · 908.9s · decision points: grain_conflict → partial · failed checks: none
5. **tot** · completed · draft · 832.2s · decision points: grain_conflict → partial · failed checks: none
6. **tot** · completed · draft · 779.7s · decision points: grain_conflict → selected · failed checks: none
