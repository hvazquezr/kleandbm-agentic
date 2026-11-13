# Northstar Retail evaluation scenario

> **This file is the answer key.** Never upload it to a project or expose it to agents.
> Agents only see `retail-discovery/` (as project documents) and the `northstar_ops`
> database (through the read-only MCP tools).

## What's here

| File | Purpose |
|---|---|
| `generate.py` | Deterministic generator (seed `20261005`) for the `northstar_ops` MySQL database |
| `load.sh` | Creates the database and the `northstar_ro` read-only user (SELECT and SHOW VIEW only), then loads the data |
| `verify.py` | Proves the source reproduces all 27 Monthly Extract cells and sizes the planted discrepancies |

```bash
eval/northstar/load.sh                                          # (re)build; password goes to backend/.env
conda run -n kleandbm-agentic python eval/northstar/verify.py   # exit 1 if any extract cell differs
```

## Source schema (`northstar_ops`)

`stores`, `product_categories`, `products`, `customers`, `orders` (UTC timestamps, status,
tax, shipping, nullable customer), `order_lines` (quantity, unit price, discount),
`refunds` (several events per line, net of discount), `store_daily_costs` (per store per day).

## Reporting conventions the extract follows

Completed orders only; gross = quantity × unit price (tax and shipping excluded); discounts per
line; returns = refunds net of discount, attributed to the original order month; snapshot as
of 2026-10-05 (refunds before 2026-10-06 00:00 UTC); outlets Web, Downtown, Riverside.
Under these conventions the source matches the workbook exactly: gross 1,001,700,
discounts 64,098, returns 38,976.

## What a good agent run should find

**Business-definition conflicts (from the documents; must go to the user, not be resolved by the agent):**
- *Net revenue*: Sales = gross − discounts (937,602); Finance = gross − discounts − returns (898,626).
  The workbook says no project-wide definition is approved.
- *Active customers*: no agreed definition, and 40% of store orders are guest checkouts
  (`orders.customer_id` NULL), so any definition must say how guests count.

**Unsupported or partially supported requirements:**
- *Profit by order*: costs exist only in `store_daily_costs` (store × day). Order-level
  profit needs an agreed allocation method; store/day profitability is a feasible proposed
  alternative, not an agreed requirement.

**Planted discrepancies (source vs. reports; should become questions):**
1. **Late refunds**: 1,351 of refunds dated after the 2026-10-05 snapshot against Q3 orders.
   Q3 returns are 38,976 at the snapshot and 40,327 today. Question: which cutoff defines
   reported returns?
2. **Undocumented store**: `stores` has Harbor (`HBR`, status `pilot`, opened 2026-09-15)
   with 64 completed September orders (gross 10,637). The reports say only Downtown and
   Riverside are physical stores. Question: is Harbor in scope, and how are pilot stores
   reported?

**Structural traps (the model must handle them):**
- `refunds` → `order_lines` is many-to-one (165 lines have two refund events). Joining
  refunds directly to lines before summing gross or discounts double-counts. Returns should
  be a separate fact at refund grain, or aggregated to line grain first.
- 133 cancelled orders, plus June and October orders, must be excluded from Q3 figures.
- Tax and shipping on `orders` are not part of gross sales.

If the generator changes, rerun `verify.py` and update the numbers above.
