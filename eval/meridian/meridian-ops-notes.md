# Meridian Supply: operations and reporting notes

Meridian Supply sells outdoor gear online (camping, climbing, footwear and apparel) and ships
orders from three distribution centers: Reno (RNO), Memphis (MEM) and Albany (ALB).

## Agreed reporting definitions

These definitions were agreed by Finance and Operations in September 2026.

- **Net sales** is quantity × (unit price − discount per unit). Tax is not recorded in the order
  system. Freight charged is billed per order and is not part of net sales.
- **Shipped net sales** is the same calculation applied to the quantity that has shipped, reported
  in the month it shipped.
- **Cancelled orders** never ship and are excluded from every sales measure.
- **Promised ship date** is set per order when the order is placed. A unit ships on time if it
  leaves on or before that date.
- **Final shipment** of an order line is the shipment that completes its ordered quantity.
- **Month-end units on hand** is the count of units in stock at a distribution center at the end of
  the last day of the month.

## Systems

The order system records orders, order lines, shipments and the warehouse management system's
nightly stock counts. All timestamps are UTC. The extract available for this project runs to
2026-10-05.
