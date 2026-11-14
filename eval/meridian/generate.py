"""Generate the Meridian Supply fulfillment database (MySQL): a scenario where the obvious
single design goes wrong and comparing structures against the data matters.

The data is synthetic and deterministic (fixed seed). Meridian sells outdoor gear online and
ships from three distribution centers (DCs). What is true of the data, and what the
agents have to find from it (the answer key is README.md, grader-side only):

1. SPLIT SHIPMENTS: an order line can ship in two or three shipments (partial quantities), and
   the lines of one order can ship from different DCs. Ship date and DC are not attributes of
   an order line, and joining order-line amounts to shipment lines repeats them.
2. BACKORDERS: some lines are not fully shipped by the extract date (2026-10-05), so "days to
   final shipment" exists only for fully shipped lines.
3. INVENTORY RE-RUNS: the nightly stock count is occasionally re-run the next morning after a
   cycle-count correction, including on two month-ends. (DC, product, date) is not unique
   across runs; the later run supersedes the earlier one. Units on hand are semi-additive.

Discounts are per unit (order_lines.discount_per_unit), so shipped net sales is
quantity_shipped x (unit_price - discount_per_unit) without any allocation.

Usage: python generate.py > meridian.sql
"""

import random
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

SEED = 20261006
START, END = date(2026, 7, 1), date(2026, 9, 30)  # order dates
EXTRACT = date(2026, 10, 5)  # data as of this date (shipments and counts after it don't exist)

DCS = [(1, "RNO", "Reno DC", "west"), (2, "MEM", "Memphis DC", "central"), (3, "ALB", "Albany DC", "east")]
REGION_DC = {"west": 1, "central": 2, "east": 3}
SHIP_LAG = {1: [0, 1, 1, 2], 2: [0, 1, 2, 2, 3], 3: [1, 2, 2, 3, 4, 5]}  # days after order, by DC
CATEGORIES = [(1, "Camping"), (2, "Climbing"), (3, "Footwear"), (4, "Apparel")]
CARRIERS = ["UPS", "FedEx", "USPS"]
RERUN_DATES = [date(2026, 7, 14), date(2026, 7, 31), date(2026, 8, 12), date(2026, 8, 31), date(2026, 9, 9),
               date(2026, 9, 22)]

SCHEMA = """
DROP TABLE IF EXISTS inventory_snapshots, inventory_snapshot_runs, shipment_lines, shipments, order_lines,
    orders, customers, products, product_categories, distribution_centers;

CREATE TABLE distribution_centers (
  dc_id     INT PRIMARY KEY,
  dc_code   CHAR(3) NOT NULL UNIQUE,
  dc_name   VARCHAR(40) NOT NULL,
  region    ENUM('west','central','east') NOT NULL
);

CREATE TABLE product_categories (
  category_id   INT PRIMARY KEY,
  category_name VARCHAR(40) NOT NULL UNIQUE
);

CREATE TABLE products (
  product_id   INT PRIMARY KEY,
  sku          VARCHAR(16) NOT NULL UNIQUE,
  product_name VARCHAR(80) NOT NULL,
  category_id  INT NOT NULL,
  list_price   DECIMAL(10,2) NOT NULL,
  CONSTRAINT fk_products_category FOREIGN KEY (category_id) REFERENCES product_categories(category_id)
);

CREATE TABLE customers (
  customer_id     INT PRIMARY KEY,
  customer_number VARCHAR(12) NOT NULL UNIQUE,
  region          ENUM('west','central','east') NOT NULL,
  signup_date     DATE NOT NULL
);

CREATE TABLE orders (
  order_id           BIGINT PRIMARY KEY,
  order_number       VARCHAR(16) NOT NULL UNIQUE,
  customer_id        INT NOT NULL,
  ordered_at         DATETIME NOT NULL COMMENT 'UTC',
  promised_ship_date DATE NOT NULL COMMENT 'Set when the order is placed',
  status             ENUM('open','completed','cancelled') NOT NULL,
  freight_charged    DECIMAL(10,2) NOT NULL,
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
  INDEX ix_orders_ordered_at (ordered_at)
);

CREATE TABLE order_lines (
  order_line_id     BIGINT PRIMARY KEY,
  order_id          BIGINT NOT NULL,
  line_number       INT NOT NULL,
  product_id        INT NOT NULL,
  quantity          INT NOT NULL,
  unit_price        DECIMAL(10,2) NOT NULL,
  discount_per_unit DECIMAL(10,2) NOT NULL DEFAULT 0,
  UNIQUE KEY uq_order_lines (order_id, line_number),
  CONSTRAINT fk_lines_order FOREIGN KEY (order_id) REFERENCES orders(order_id),
  CONSTRAINT fk_lines_product FOREIGN KEY (product_id) REFERENCES products(product_id)
);

CREATE TABLE shipments (
  shipment_id BIGINT PRIMARY KEY,
  order_id    BIGINT NOT NULL,
  dc_id       INT NOT NULL,
  shipped_at  DATETIME NOT NULL COMMENT 'UTC',
  carrier     VARCHAR(12) NOT NULL,
  CONSTRAINT fk_shipments_order FOREIGN KEY (order_id) REFERENCES orders(order_id),
  CONSTRAINT fk_shipments_dc FOREIGN KEY (dc_id) REFERENCES distribution_centers(dc_id),
  INDEX ix_shipments_shipped_at (shipped_at)
);

CREATE TABLE shipment_lines (
  shipment_id       BIGINT NOT NULL,
  order_line_id     BIGINT NOT NULL,
  quantity_shipped  INT NOT NULL,
  PRIMARY KEY (shipment_id, order_line_id),
  CONSTRAINT fk_shiplines_shipment FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id),
  CONSTRAINT fk_shiplines_line FOREIGN KEY (order_line_id) REFERENCES order_lines(order_line_id)
);

CREATE TABLE inventory_snapshot_runs (
  run_id        INT PRIMARY KEY,
  snapshot_date DATE NOT NULL COMMENT 'The business day the count describes',
  captured_at   DATETIME NOT NULL COMMENT 'UTC',
  note          VARCHAR(80) NULL
);

CREATE TABLE inventory_snapshots (
  run_id         INT NOT NULL,
  dc_id          INT NOT NULL,
  product_id     INT NOT NULL,
  units_on_hand  INT NOT NULL,
  PRIMARY KEY (run_id, dc_id, product_id),
  CONSTRAINT fk_inv_run FOREIGN KEY (run_id) REFERENCES inventory_snapshot_runs(run_id),
  CONSTRAINT fk_inv_dc FOREIGN KEY (dc_id) REFERENCES distribution_centers(dc_id),
  CONSTRAINT fk_inv_product FOREIGN KEY (product_id) REFERENCES products(product_id)
) COMMENT='Nightly stock counts per DC and product';
"""


@dataclass
class Line:
    line_id: int
    product_id: int
    quantity: int
    unit_price: float
    discount: float
    dc_id: int
    parts: list[tuple[int, int]] = field(default_factory=list)  # (days after order, quantity); unshipped rest is open


@dataclass
class Order:
    order_id: int
    customer_id: int
    ordered_at: datetime
    promised: date
    status: str
    freight: float
    lines: list[Line] = field(default_factory=list)


class Builder:
    def __init__(self) -> None:
        self.rng = random.Random(SEED)
        self.products: list[tuple] = []
        self.customers: list[tuple] = []
        self.orders: list[Order] = []
        self.facts: dict[str, int] = {}

    def build(self) -> dict[str, int]:
        r = self.rng
        names = {1: ["Tent", "Sleeping Bag", "Stove", "Lantern", "Camp Chair", "Cooler", "Water Filter", "Tarp"],
                 2: ["Harness", "Rope 60m", "Carabiner Set", "Belay Device", "Chalk Bag", "Helmet", "Quickdraws", "Crash Pad"],
                 3: ["Trail Runner", "Hiking Boot", "Approach Shoe", "Sandal", "Gaiter", "Wool Sock", "Insole", "Camp Shoe"],
                 4: ["Rain Shell", "Fleece", "Down Jacket", "Base Layer", "Hiking Pant", "Sun Hoody", "Beanie", "Glove"]}
        pid = 1
        for cat, items in names.items():
            for name in items:
                price = round(r.choice([24, 39, 59, 89, 129, 179, 249]) + 0.95, 2)
                self.products.append((pid, f"MS-{cat}{pid:03d}", name, cat, price))
                pid += 1
        for cid in range(1, 451):
            region = r.choices(["west", "central", "east"], [0.4, 0.3, 0.3])[0]
            self.customers.append((cid, f"C{cid:06d}", region, START - timedelta(days=r.randint(10, 900))))
        region_of = {c[0]: c[2] for c in self.customers}

        oid, lid = 500001, 1
        day = START
        while day <= END:
            for _ in range(r.randint(12, 19)):
                cust = r.randint(1, len(self.customers))
                at = datetime.combine(day, time(r.randint(0, 23), r.randint(0, 59)))
                status = "cancelled" if r.random() < 0.04 else "completed"
                order = Order(oid, cust, at, day + timedelta(days=2), status,
                              round(r.choice([0, 0, 6.95, 9.95, 14.95]), 2))
                home = REGION_DC[region_of[cust]]
                for n in range(r.choices([1, 2, 3, 4], [0.45, 0.3, 0.17, 0.08])[0]):
                    p = r.choice(self.products)
                    qty = r.choices([1, 2, 3, 4, 6], [0.45, 0.25, 0.15, 0.1, 0.05])[0]
                    disc = round(p[4] * r.choice([0, 0, 0, 0.1, 0.15]), 2)
                    # Some lines of a multi-line order come from another DC (stock elsewhere).
                    dc = home if n == 0 or r.random() > 0.25 else r.choice([d for d in (1, 2, 3) if d != home])
                    order.lines.append(Line(lid, p[0], qty, p[4], disc, dc))
                    lid += 1
                if status == "completed":
                    self._ship(order)
                self.orders.append(order)
                oid += 1
            day += timedelta(days=1)
        self._close_statuses()
        return self.facts

    def _ship(self, order: Order) -> None:
        r = self.rng
        for line in order.lines:
            first = r.choice(SHIP_LAG[line.dc_id])
            if line.quantity >= 2 and r.random() < 0.18:  # split: part now, the rest from backorder
                now = r.randint(1, line.quantity - 1)
                line.parts.append((first, now))
                rest = line.quantity - now
                if line.quantity >= 4 and r.random() < 0.3:  # a third shipment
                    mid = r.randint(1, rest - 1) if rest > 1 else 0
                    if mid:
                        line.parts.append((first + r.randint(2, 6), mid))
                        rest -= mid
                if r.random() > 0.12:  # most backorders are filled; some stay open
                    line.parts.append((first + r.randint(4, 14), rest))
            else:
                line.parts.append((first, line.quantity))
            # Nothing ships after the extract date: that quantity is still open.
            line.parts = [(d, q) for d, q in line.parts if order.ordered_at.date() + timedelta(days=d) <= EXTRACT]

    def _close_statuses(self) -> None:
        for o in self.orders:
            if o.status == "completed" and any(sum(q for _, q in l.parts) < l.quantity for l in o.lines):
                o.status = "open"

    def shipments(self) -> tuple[list[tuple], list[tuple]]:
        """Group the line parts into shipments: one per order, DC and ship day."""
        r = random.Random(SEED + 1)
        ships, ship_lines, sid = [], [], 900001
        for o in self.orders:
            groups: dict[tuple[int, int], dict[int, int]] = {}
            for l in o.lines:
                for d, q in l.parts:  # parts of one line on the same day travel together
                    items = groups.setdefault((l.dc_id, d), {})
                    items[l.line_id] = items.get(l.line_id, 0) + q
            for (dc, d), items in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
                when = datetime.combine(o.ordered_at.date() + timedelta(days=d), time(r.randint(8, 19), r.randint(0, 59)))
                if when <= o.ordered_at:
                    when = o.ordered_at + timedelta(hours=r.randint(2, 6))
                ships.append((sid, o.order_id, dc, when, r.choice(CARRIERS)))
                ship_lines += [(sid, line_id, q) for line_id, q in items.items()]
                sid += 1
        return ships, ship_lines

    def inventory(self, ships: list[tuple], ship_lines: list[tuple]) -> tuple[list[tuple], list[tuple]]:
        r = random.Random(SEED + 2)
        line_product = {l.line_id: l.product_id for o in self.orders for l in o.lines}
        ship_info = {s[0]: (s[2], s[3].date()) for s in ships}
        out: dict[tuple[date, int, int], int] = {}
        for sid, line_id, q in ship_lines:
            dc, d = ship_info[sid]
            key = (d, dc, line_product[line_id])
            out[key] = out.get(key, 0) + q
        on_hand = {(dc, p[0]): r.randint(40, 160) for dc, *_ in DCS for p in self.products}
        runs, rows, run_id = [], [], 1
        day = START
        while day <= EXTRACT:
            for key in on_hand:
                on_hand[key] = max(0, on_hand[key] - out.get((day, *key), 0))
                if day.weekday() == 0:  # weekly replenishment
                    on_hand[key] += r.randint(10, 40)
            runs.append((run_id, day, datetime.combine(day, time(23, 30)), None))
            rows += [(run_id, dc, pid, n) for (dc, pid), n in on_hand.items()]
            run_id += 1
            if day in RERUN_DATES:  # a cycle count corrected one DC: re-run next morning
                dc_fixed = r.choice([1, 2, 3])
                runs.append((run_id, day, datetime.combine(day + timedelta(days=1), time(6, 10)),
                             "recount after cycle-count correction"))
                for (dc, pid), n in on_hand.items():
                    if dc == dc_fixed and r.random() < 0.4:
                        n = on_hand[(dc, pid)] = max(0, n + r.choice([-6, -4, -3, -2, 2, 3]))
                    rows.append((run_id, dc, pid, n))
                run_id += 1
            day += timedelta(days=1)
        self.facts["rerun_days"] = len(RERUN_DATES)
        return runs, rows


def sql_value(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, datetime):
        return f"'{v:%Y-%m-%d %H:%M:%S}'"
    if isinstance(v, date):
        return f"'{v:%Y-%m-%d}'"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def inserts(table: str, columns: list[str], rows: list[tuple], batch: int = 500) -> str:
    out = []
    for i in range(0, len(rows), batch):
        values = ",\n".join("(" + ",".join(sql_value(v) for v in row) + ")" for row in rows[i : i + batch])
        out.append(f"INSERT INTO {table} ({', '.join(columns)}) VALUES\n{values};")
    return "\n".join(out)


def render(b: Builder) -> str:
    ships, ship_lines = b.shipments()
    runs, inv = b.inventory(ships, ship_lines)
    lines = [(l.line_id, o.order_id, n + 1, l.product_id, l.quantity, l.unit_price, l.discount)
             for o in b.orders for n, l in enumerate(o.lines)]
    split = sum(1 for o in b.orders for l in o.lines if len({d for d, _ in l.parts}) > 1)
    multi_dc = sum(1 for o in b.orders if len({l.dc_id for l in o.lines if l.parts}) > 1)
    open_lines = sum(1 for o in b.orders if o.status != "cancelled" for l in o.lines if sum(q for _, q in l.parts) < l.quantity)
    b.facts.update(orders=len(b.orders), lines=len(lines), shipments=len(ships), split_lines=split,
                   multi_dc_orders=multi_dc, open_lines=open_lines)
    parts = [
        "SET NAMES utf8mb4;",
        SCHEMA,
        inserts("distribution_centers", ["dc_id", "dc_code", "dc_name", "region"], DCS),
        inserts("product_categories", ["category_id", "category_name"], CATEGORIES),
        inserts("products", ["product_id", "sku", "product_name", "category_id", "list_price"], b.products),
        inserts("customers", ["customer_id", "customer_number", "region", "signup_date"], b.customers),
        inserts("orders", ["order_id", "order_number", "customer_id", "ordered_at", "promised_ship_date", "status",
                           "freight_charged"],
                [(o.order_id, f"MS-{o.order_id}", o.customer_id, o.ordered_at, o.promised, o.status, o.freight)
                 for o in b.orders]),
        inserts("order_lines", ["order_line_id", "order_id", "line_number", "product_id", "quantity", "unit_price",
                                "discount_per_unit"], lines),
        inserts("shipments", ["shipment_id", "order_id", "dc_id", "shipped_at", "carrier"], ships),
        inserts("shipment_lines", ["shipment_id", "order_line_id", "quantity_shipped"], ship_lines),
        inserts("inventory_snapshot_runs", ["run_id", "snapshot_date", "captured_at", "note"], runs),
        inserts("inventory_snapshots", ["run_id", "dc_id", "product_id", "units_on_hand"], inv),
    ]
    return "\n\n".join(parts) + "\n"


if __name__ == "__main__":
    builder = Builder()
    builder.build()
    sys.stdout.write(render(builder))
    print("-- " + ", ".join(f"{k} {v}" for k, v in builder.facts.items()), file=sys.stderr)
