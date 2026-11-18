# Baseline comparison: single candidate vs. selective ToT (meridian)

Runs per variant: baseline 3, tot 3. Values are means over runs; rubric rows count the runs that passed. Rubric checks are keyword heuristics over the records (eval/baseline/grade.py), so check surprising rows in the run JSON.

| | baseline | tot |
|---|---|---|
| completed runs | 3/3 | 3/3 |
| **rubric score** (share of checks passed) | 0.97 | 0.97 |
| &nbsp;&nbsp;facts_declare_grain | 3/3 | 3/3 |
| &nbsp;&nbsp;inventory_reruns_handled | 3/3 | 3/3 |
| &nbsp;&nbsp;month_end_stock_feasible | 3/3 | 3/3 |
| &nbsp;&nbsp;net_sales_feasible | 3/3 | 3/3 |
| &nbsp;&nbsp;no_shipment_attributes_on_orders_or_lines | 3/3 | 3/3 |
| &nbsp;&nbsp;on_time_feasible | 3/3 | 3/3 |
| &nbsp;&nbsp;open_lines_handled | 2/3 | 2/3 |
| &nbsp;&nbsp;shipment_grain_fact | 3/3 | 3/3 |
| &nbsp;&nbsp;shipped_sales_feasible | 3/3 | 3/3 |
| &nbsp;&nbsp;shipped_sales_from_shipped_quantity | 3/3 | 3/3 |
| &nbsp;&nbsp;stock_semi_additive | 3/3 | 3/3 |
| Ready for Review | 0/3 | 0/3 |
| readiness blockers | 2.67 | 2.33 |
| Validator passed / failed / unverified | 11.0 / 0.0 / 3.67 | 10.0 / 0.0 / 3.33 |
| review rounds | 1.67 | 1.33 |
| tables | 7.0 | 7.0 |
| questions raised | 3.0 | 2.0 |
| decision points (total) | 0 | 0 |
| search outcomes | – | – |
| seconds | 508.07 | 439.33 |
| model calls (all agents) | 42.33 | 33.33 |
| input tokens | 331675.67 | 274659.0 |
| output tokens | 48694.67 | 42101.33 |
| &nbsp;&nbsp;of which in searches | 0.0 | 0.0 |
| cost (USD) | – (pass --prices) | – (pass --prices) |

## Runs

1. **baseline** · completed · draft · 449.4s · decision points: none · failed checks: open_lines_handled
2. **tot** · completed · draft · 382.8s · decision points: none · failed checks: none
3. **baseline** · completed · draft · 566.6s · decision points: none · failed checks: none
4. **tot** · completed · draft · 391.4s · decision points: none · failed checks: open_lines_handled
5. **baseline** · completed · draft · 508.2s · decision points: none · failed checks: none
6. **tot** · completed · draft · 543.8s · decision points: none · failed checks: none
