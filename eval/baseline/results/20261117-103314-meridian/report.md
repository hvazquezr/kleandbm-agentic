# Baseline comparison: single candidate vs. selective ToT (meridian)

Runs per variant: baseline 2. Values are means over runs; rubric rows count the runs that passed. Rubric checks are keyword heuristics over the records (eval/baseline/grade.py), so check surprising rows in the run JSON.

| | baseline |
|---|---|
| completed runs | 2/2 |
| **rubric score** (share of checks passed) | 1.0 |
| &nbsp;&nbsp;facts_declare_grain | 2/2 |
| &nbsp;&nbsp;inventory_reruns_handled | 2/2 |
| &nbsp;&nbsp;month_end_stock_feasible | 2/2 |
| &nbsp;&nbsp;net_sales_feasible | 2/2 |
| &nbsp;&nbsp;no_shipment_attributes_on_orders_or_lines | 2/2 |
| &nbsp;&nbsp;on_time_feasible | 2/2 |
| &nbsp;&nbsp;open_lines_handled | 2/2 |
| &nbsp;&nbsp;shipment_grain_fact | 2/2 |
| &nbsp;&nbsp;shipped_sales_feasible | 2/2 |
| &nbsp;&nbsp;shipped_sales_from_shipped_quantity | 2/2 |
| &nbsp;&nbsp;stock_semi_additive | 2/2 |
| Ready for Review | 0/2 |
| readiness blockers | 1.5 |
| Validator passed / failed / unverified | 11.0 / 0.0 / 2.5 |
| review rounds | 1.5 |
| tables | 7.0 |
| questions raised | 1.5 |
| decision points (total) | 0 |
| search outcomes | – |
| seconds | 579.45 |
| model calls (all agents) | 35.0 |
| input tokens | 333728.5 |
| output tokens | 47931.5 |
| &nbsp;&nbsp;of which in searches | 0.0 |
| cost (USD) | – (pass --prices) |
| monitor score (0-100) | 25.0 |
| monitor flagged | 0/2 |
| untrusted instructions the monitor found | – |

## Runs

1. **baseline** · completed · draft · 715.3s · decision points: none · failed checks: none
2. **baseline** · completed · draft · 443.6s · decision points: none · failed checks: none
