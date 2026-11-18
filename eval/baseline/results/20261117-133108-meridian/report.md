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
| readiness blockers | 4.0 |
| Validator passed / failed / unverified | 13.0 / 0.0 / 4.0 |
| review rounds | 2.5 |
| tables | 7.0 |
| questions raised | 3.5 |
| decision points (total) | 0 |
| search outcomes | – |
| seconds | 835.6 |
| model calls (all agents) | 48.5 |
| input tokens | 633213.5 |
| output tokens | 62040.0 |
| &nbsp;&nbsp;of which in searches | 0.0 |
| cost (USD) | – (pass --prices) |
| monitor score (0-100) | 23.5 |
| monitor flagged | 0/2 |
| untrusted instructions the monitor found | – |

## Runs

1. **baseline** · completed · draft · 808.7s · decision points: none · failed checks: none
2. **baseline** · completed · draft · 862.5s · decision points: none · failed checks: none
