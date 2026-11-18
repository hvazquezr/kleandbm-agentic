# Baseline comparison: single candidate vs. selective ToT (northstar_structures)

Runs per variant: tot 3. Values are means over runs; rubric rows count the runs that passed. Rubric checks are keyword heuristics over the records (eval/baseline/grade.py), so check surprising rows in the run JSON.

| | tot |
|---|---|
| completed runs | 3/3 |
| **rubric score** (share of checks passed) | 1.0 |
| &nbsp;&nbsp;facts_declare_grain | 3/3 |
| &nbsp;&nbsp;guest_checkouts_handled | 3/3 |
| &nbsp;&nbsp;no_order_measures_on_lines | 3/3 |
| &nbsp;&nbsp;order_grain_fact_for_order_measures | 3/3 |
| &nbsp;&nbsp;order_value_feasible | 3/3 |
| &nbsp;&nbsp;refund_rate_feasible | 3/3 |
| &nbsp;&nbsp;returns_fact_separate | 3/3 |
| &nbsp;&nbsp;sales_by_both_stores_feasible | 3/3 |
| &nbsp;&nbsp;shipping_by_category_honest | 3/3 |
| &nbsp;&nbsp;store_roles_distinguished | 3/3 |
| Ready for Review | 0/3 |
| readiness blockers | 3.33 |
| Validator passed / failed / unverified | 12.33 / 0.0 / 3.0 |
| review rounds | 2.0 |
| tables | 8.0 |
| questions raised | 3.67 |
| decision points (total) | 3 |
| search outcomes | partial 2, selected 1 |
| seconds | 840.27 |
| model calls (all agents) | 46.33 |
| input tokens | 434886.67 |
| output tokens | 78957.0 |
| &nbsp;&nbsp;of which in searches | 29860.0 |
| cost (USD) | – (pass --prices) |

## Runs

1. **tot** · completed · draft · 908.9s · decision points: grain_conflict → partial · failed checks: none
2. **tot** · completed · draft · 832.2s · decision points: grain_conflict → partial · failed checks: none
3. **tot** · completed · draft · 779.7s · decision points: grain_conflict → selected · failed checks: none
