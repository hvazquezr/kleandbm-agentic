# Baseline comparison: single candidate vs. selective ToT

Runs per variant: baseline 3, tot 3. Values are means over runs; rubric rows count the runs that passed. Rubric checks are keyword heuristics over the records (eval/baseline/grade.py), so check surprising rows in the run JSON.

| | baseline | tot |
|---|---|---|
| completed runs | 3/3 | 3/3 |
| **rubric score** (share of checks passed) | 0.89 | 0.85 |
| &nbsp;&nbsp;facts_declare_grain | 3/3 | 3/3 |
| &nbsp;&nbsp;harbor_store_question | 1/3 | 1/3 |
| &nbsp;&nbsp;late_refunds_question | 2/3 | 1/3 |
| &nbsp;&nbsp;net_revenue_conflict_to_user | 3/3 | 3/3 |
| &nbsp;&nbsp;no_order_level_cost_allocation | 3/3 | 3/3 |
| &nbsp;&nbsp;profit_by_order_honest | 3/3 | 3/3 |
| &nbsp;&nbsp;returns_fact_separate | 3/3 | 3/3 |
| &nbsp;&nbsp;revenue_and_returns_feasible | 3/3 | 3/3 |
| &nbsp;&nbsp;store_day_alternative_offered | 3/3 | 3/3 |
| Ready for Review | 0/3 | 0/3 |
| readiness blockers | 2.33 | 1.33 |
| Validator passed / failed / unverified | 10.33 / 0.0 / 3.0 | 10.33 / 0.0 / 1.67 |
| review rounds | 1.33 | 1.67 |
| tables | 5.67 | 6.33 |
| questions raised | 3.33 | 2.0 |
| decision points (total) | 0 | 3 |
| search outcomes | – | partial 1, selected 2 |
| seconds | 396.3 | 546.33 |
| model calls (all agents) | 33.33 | 36.33 |
| input tokens | 221568.33 | 274192.0 |
| output tokens | 35308.67 | 49679.0 |
| &nbsp;&nbsp;of which in searches | 0.0 | 13027.33 |
| cost (USD) | – (pass --prices) | – (pass --prices) |

## Runs

1. **baseline** · completed · draft · 380.3s · decision points: none · failed checks: late_refunds_question
2. **tot** · completed · draft · 545.6s · decision points: grain_conflict → selected · failed checks: late_refunds_question, harbor_store_question
3. **baseline** · completed · draft · 453.0s · decision points: none · failed checks: harbor_store_question
4. **tot** · completed · draft · 497.7s · decision points: grain_conflict → selected · failed checks: late_refunds_question
5. **baseline** · completed · draft · 355.6s · decision points: none · failed checks: harbor_store_question
6. **tot** · completed · draft · 595.7s · decision points: grain_conflict → partial · failed checks: harbor_store_question
