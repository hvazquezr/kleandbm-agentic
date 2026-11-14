# Meridian Supply: answer key (grader-side only)

**Never upload this file to a project or show it to an agent.** Agents get the
`meridian_ops` source (read-only user `meridian_ro`) and `meridian-ops-notes.md` only.

Build it with `eval/meridian/load.sh` (Homebrew MySQL; the read-only password is saved as
`KLEANDBM_DEV_MERIDIAN_PASSWORD` in `backend/.env` and never printed). The data is synthetic and
deterministic (`generate.py`, seed 20261006): 1,415 orders, 2,674 order lines, 2,448 shipments,
orders from 2026-07-01 to 2026-09-30, data as of 2026-10-05.

This scenario is built so the obvious single design goes wrong, and checking alternatives against
the data catches it. The conflicts are technical, not business meaning: the notes define every
measure.

## Requirements given to the agents

1. Monthly net sales by product category and order month
2. Monthly shipped net sales by distribution center and product category, by ship month
3. On-time shipping rate by distribution center and month: the share of shipped units that left on
   or before the order's promised ship date
4. Average days from order to final shipment for each order line, by product category
5. Month-end units on hand by distribution center and product category

## What the data holds

| Trap | Size | The wrong design | The right one |
|---|---|---|---|
| Split shipments | 204 order lines ship on more than one day | A ship date on the order-line fact (it picks one of several) | A shipment-line fact (one row per shipment and order line) with `quantity_shipped` |
| Multi-DC orders | 262 orders ship from more than one DC (each line ships from one DC) | DC on an order-grain fact | DC on the shipment (or the line, derived from its shipments) |
| Fan-out | Shipped net is 514,748.44; joining order-line amounts to shipment lines gives 589,209.48 (+14.5%) | Shipped sales from the order-line amount | `quantity_shipped × (unit_price − discount_per_unit)`, no allocation needed |
| Backorders | 41 lines not fully shipped by 2026-10-05 | Final ship date taken as the latest shipment so far | Days to final shipment only for fully shipped lines; requirement 4 partly supported (or open lines excluded and said so). An accumulating snapshot per order line fits |
| Inventory re-runs | 6 days have two count runs, including 2026-07-31 and 2026-08-31 | (DC, product, date) treated as the grain: month-end stock doubles (32,827 vs. 16,411 for July) | Grain includes the run, or only the latest run per date (the later run is a correction) |
| Semi-additive stock | `units_on_hand` | Marked additive, summed over days | Semi-additive: sum across DCs and products, never across dates; month-end = the last day's count |

On-time share of shipped units: RNO 94.0%, MEM 73.4%, ALB 47.1%. Average days to final shipment for
fully shipped lines: 2.46.

## Where selective ToT should help

- **Shipment structure** (competing requirements 1, 2, 3 and 4): an order-line fact, a shipment-line
  fact, an accumulating snapshot per order line, or a combination. Requirement 2 and 3 need the
  shipment-line grain; 4 fits an accumulating snapshot; 1 is at order-line grain.
- **Inventory grain** (grain conflict or ambiguous evidence): per run, or the latest run per date.

The rubric is `grade_meridian` in `eval/baseline/grade.py`.
