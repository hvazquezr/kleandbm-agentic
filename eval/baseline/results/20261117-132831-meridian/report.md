# Baseline comparison: single candidate vs. selective ToT (meridian)

Runs per variant: baseline 2. Values are means over runs; rubric rows count the runs that passed. Rubric checks are keyword heuristics over the records (eval/baseline/grade.py), so check surprising rows in the run JSON.

| | baseline |
|---|---|
| completed runs | 0/2 |
| **rubric score** (share of checks passed) | 0.09 |
| &nbsp;&nbsp;facts_declare_grain | 0/2 |
| &nbsp;&nbsp;inventory_reruns_handled | 0/2 |
| &nbsp;&nbsp;month_end_stock_feasible | 0/2 |
| &nbsp;&nbsp;net_sales_feasible | 0/2 |
| &nbsp;&nbsp;no_shipment_attributes_on_orders_or_lines | 2/2 |
| &nbsp;&nbsp;on_time_feasible | 0/2 |
| &nbsp;&nbsp;open_lines_handled | 0/2 |
| &nbsp;&nbsp;shipment_grain_fact | 0/2 |
| &nbsp;&nbsp;shipped_sales_feasible | 0/2 |
| &nbsp;&nbsp;shipped_sales_from_shipped_quantity | 0/2 |
| &nbsp;&nbsp;stock_semi_additive | 0/2 |
| Ready for Review | 0/2 |
| readiness blockers | 1.0 |
| Validator passed / failed / unverified | None / None / None |
| review rounds | 0.0 |
| tables | 0.0 |
| questions raised | 0.0 |
| decision points (total) | 0 |
| search outcomes | – |
| seconds | 0.4 |
| model calls (all agents) | 0.0 |
| input tokens | 0.0 |
| output tokens | 0.0 |
| &nbsp;&nbsp;of which in searches | 0.0 |
| cost (USD) | – (pass --prices) |
| monitor score (0-100) | 0.0 |
| monitor flagged | 0/2 |
| untrusted instructions the monitor found | – |

## Runs

1. **baseline** · failed · draft · 0.5s · decision points: none · failed checks: facts_declare_grain, shipment_grain_fact, shipped_sales_from_shipped_quantity, open_lines_handled, stock_semi_additive, inventory_reruns_handled, net_sales_feasible, shipped_sales_feasible, on_time_feasible, month_end_stock_feasible
2. **baseline** · failed · draft · 0.3s · decision points: none · failed checks: facts_declare_grain, shipment_grain_fact, shipped_sales_from_shipped_quantity, open_lines_handled, stock_semi_additive, inventory_reruns_handled, net_sales_feasible, shipped_sales_feasible, on_time_feasible, month_end_stock_feasible
