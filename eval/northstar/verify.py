"""Check that the Northstar source reproduces the discovery workbook, and size the planted
discrepancies.

Connects as the read-only user (password from KLEANDBM_DEV_NORTHSTAR_PASSWORD in the
environment or backend/.env) and reads the Monthly Extract sheet from the workbook.

Usage: python eval/northstar/verify.py   (exit code 1 if any extract cell doesn't match)
"""

import os
import sys
from decimal import Decimal
from pathlib import Path

import pymysql
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[2]
WORKBOOK = ROOT / "retail-discovery" / "northstar-current-state-report.xlsx"

# Same conventions as the extract: completed orders, Q3 order months, known outlets, and
# refunds before the snapshot instant, attributed to the original order month.
CELL_SQL = """
SELECT DATE_FORMAT(o.ordered_at, '%%Y-%%m') AS month, s.store_name AS outlet, c.category_name AS category,
       SUM(l.quantity * l.unit_price) AS gross,
       SUM(l.discount_amount) AS discounts,
       COALESCE(SUM(r.refunded), 0) AS returns_amt
FROM orders o
JOIN stores s ON s.store_id = o.store_id
JOIN order_lines l ON l.order_id = o.order_id
JOIN products p ON p.product_id = l.product_id
JOIN product_categories c ON c.category_id = p.category_id
LEFT JOIN (SELECT order_line_id, SUM(refund_amount) AS refunded FROM refunds
           WHERE refunded_at < %(cutoff)s GROUP BY order_line_id) r ON r.order_line_id = l.order_line_id
WHERE o.status = 'completed' AND o.ordered_at >= '2026-07-01' AND o.ordered_at < '2026-10-01'
  AND s.store_name IN %(outlets)s
GROUP BY month, outlet, category
"""


def password() -> str:
    if value := os.environ.get("KLEANDBM_DEV_NORTHSTAR_PASSWORD"):
        return value
    for line in (ROOT / "backend" / ".env").read_text().splitlines():
        if line.startswith("KLEANDBM_DEV_NORTHSTAR_PASSWORD="):
            return line.split("=", 1)[1].strip()
    sys.exit("KLEANDBM_DEV_NORTHSTAR_PASSWORD not set; run eval/northstar/load.sh first")


def workbook_cells() -> dict[tuple[str, str, str], tuple[int, int, int]]:
    ws = load_workbook(WORKBOOK, data_only=True)["Monthly Extract"]
    cells = {}
    for row in ws.iter_rows(min_row=8, max_row=34, min_col=2, max_col=9, values_only=True):
        _, month, _, outlet, category, gross, discounts, returns = row
        cells[(month.strftime("%Y-%m"), outlet, category)] = (gross, discounts, returns)
    return cells


def source_cells(cur, cutoff: str, outlets: tuple[str, ...]) -> dict:
    cur.execute(CELL_SQL, {"cutoff": cutoff, "outlets": outlets})
    return {(m, o, c): (int(g), int(d), int(r)) for m, o, c, g, d, r in cur.fetchall()}


def main() -> int:
    conn = pymysql.connect(host="127.0.0.1", user="northstar_ro", password=password(), database="northstar_ops")
    cur = conn.cursor()
    expected = workbook_cells()
    snapshot = source_cells(cur, "2026-10-06", ("Web", "Downtown", "Riverside"))

    mismatches = [k for k in expected if snapshot.get(k) != expected[k]]
    extra = sorted(set(snapshot) - set(expected))
    print(f"Extract cells: {len(expected)}; reproduced exactly: {len(expected) - len(mismatches)}")
    for k in mismatches:
        print(f"  MISMATCH {k}: workbook {expected[k]} vs source {snapshot.get(k)}")
    totals = [sum(v[i] for v in snapshot.values()) for i in range(3)]
    print(f"Snapshot totals: gross {totals[0]:,}  discounts {totals[1]:,}  returns {totals[2]:,}")

    # Discrepancy 1: refunds known today vs at the snapshot.
    today = source_cells(cur, "2100-01-01", ("Web", "Downtown", "Riverside"))
    late = sum(v[2] for v in today.values()) - totals[2]
    print(f"\n[1] Late refunds against Q3 orders (after 2026-10-05): {late:,}"
          f" -> Q3 returns {totals[2]:,} at snapshot vs {totals[2] + late:,} today")

    # Discrepancy 2: completed Q3 orders at stores the reports don't list.
    cur.execute("""SELECT s.store_name, s.status, s.opened_on, COUNT(DISTINCT o.order_id),
                          SUM(l.quantity * l.unit_price)
                   FROM orders o JOIN stores s ON s.store_id = o.store_id
                   JOIN order_lines l ON l.order_id = o.order_id
                   WHERE o.status = 'completed' AND o.ordered_at >= '2026-07-01' AND o.ordered_at < '2026-10-01'
                     AND s.store_name NOT IN ('Web','Downtown','Riverside')
                   GROUP BY s.store_name, s.status, s.opened_on""")
    for name, status, opened, orders, gross in cur.fetchall():
        print(f"[2] Unreported store {name} ({status}, opened {opened}): {orders} completed Q3 orders,"
              f" gross {Decimal(gross):,.0f}")

    # Natural ambiguities, for the record.
    cur.execute("SELECT SUM(customer_id IS NULL), COUNT(*) FROM orders WHERE status='completed' AND store_id<>1")
    guests, store_orders = cur.fetchone()
    cur.execute("""SELECT COUNT(*) FROM (SELECT order_line_id FROM refunds GROUP BY order_line_id
                   HAVING COUNT(*) > 1) x""")
    multi = cur.fetchone()[0]
    print(f"\nGuest (no customer) store orders: {guests} of {store_orders}")
    print(f"Order lines with more than one refund event: {multi}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
