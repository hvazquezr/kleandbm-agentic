"""Generate the Northstar Retail operational database (MySQL) behind the discovery workbook.

The data is synthetic and deterministic (fixed seed). It is built so that, under the
workbook's own reporting conventions, it reproduces every one of the 27 Monthly Extract
cells exactly (order month x outlet x category: gross, discounts, returns).

Reporting conventions the extract follows, which the agents have to discover:
- completed orders only (cancelled orders exist and are excluded)
- gross = quantity x unit price; tax and shipping excluded
- discounts per order line; returns are refunds net of discount, attributed to the
  original order month; several refund events can hit one order line
- the extract is a snapshot as of 2026-10-05: refunds after that date are not in it
- outlets are Web, Downtown, Riverside

Deliberate discrepancies the agents should find and raise as questions:
1. LATE REFUNDS: refunds dated after the 2026-10-05 snapshot against Q3 orders. Source
   returns for Q3 no longer match the reports unless a cutoff is applied.
2. UNDOCUMENTED STORE: a "Harbor" pilot store (opened 2026-09-15) has completed September
   orders. The reports say only Downtown and Riverside are physical stores.

Natural ambiguities (not planted, just true of the data):
- store purchases often have no customer (guest checkout), so "active customers" needs a
  definition
- costs exist only per store per day, so "profit by order" is unsupported without an
  agreed allocation method

Usage: python generate.py > northstar.sql
"""

import random
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

SEED = 20261005
SNAPSHOT_CUTOFF = datetime(2026, 10, 6)  # refunds before this instant are in the extract

# Monthly Extract targets: (month, outlet, category) -> (gross, discounts, returns)
TARGETS = {
    ("2026-07", "Web", "Apparel"): (54000, 4320, 2850),
    ("2026-07", "Web", "Home"): (43500, 2610, 1508),
    ("2026-07", "Web", "Accessories"): (34500, 1380, 938),
    ("2026-07", "Downtown", "Apparel"): (42000, 3360, 2010),
    ("2026-07", "Downtown", "Home"): (31500, 1890, 908),
    ("2026-07", "Downtown", "Accessories"): (22500, 900, 458),
    ("2026-07", "Riverside", "Apparel"): (36000, 2880, 1740),
    ("2026-07", "Riverside", "Home"): (25500, 1530, 758),
    ("2026-07", "Riverside", "Accessories"): (16500, 660, 368),
    ("2026-08", "Web", "Apparel"): (58500, 4680, 3113),
    ("2026-08", "Web", "Home"): (46500, 2790, 1643),
    ("2026-08", "Web", "Accessories"): (36300, 1452, 1025),
    ("2026-08", "Downtown", "Apparel"): (46500, 3720, 2273),
    ("2026-08", "Downtown", "Home"): (34500, 2070, 1043),
    ("2026-08", "Downtown", "Accessories"): (24300, 972, 545),
    ("2026-08", "Riverside", "Apparel"): (40500, 3240, 2003),
    ("2026-08", "Riverside", "Home"): (28500, 1710, 893),
    ("2026-08", "Riverside", "Accessories"): (18300, 732, 455),
    ("2026-09", "Web", "Apparel"): (63000, 5040, 3375),
    ("2026-09", "Web", "Home"): (49500, 2970, 1778),
    ("2026-09", "Web", "Accessories"): (38100, 1524, 1112),
    ("2026-09", "Downtown", "Apparel"): (51000, 4080, 2535),
    ("2026-09", "Downtown", "Home"): (37500, 2250, 1178),
    ("2026-09", "Downtown", "Accessories"): (26100, 1044, 632),
    ("2026-09", "Riverside", "Apparel"): (45000, 3600, 2265),
    ("2026-09", "Riverside", "Home"): (31500, 1890, 1028),
    ("2026-09", "Riverside", "Accessories"): (20100, 804, 542),
}

STORES = [
    # id, code, name, channel, status, opened_on
    (1, "WEB", "Web", "online", "active", date(2019, 3, 1)),
    (2, "DTN", "Downtown", "store", "active", date(2015, 5, 16)),
    (3, "RVS", "Riverside", "store", "active", date(2017, 9, 2)),
    (4, "HBR", "Harbor", "store", "pilot", date(2026, 9, 15)),  # discrepancy 2
]
OUTLET_STORE = {"Web": 1, "Downtown": 2, "Riverside": 3, "Harbor": 4}
CATEGORIES = [(1, "Apparel"), (2, "Home"), (3, "Accessories")]
CATEGORY_ID = {name: cid for cid, name in CATEGORIES}
PRICE_RANGES = {"Apparel": (18, 140), "Home": (12, 220), "Accessories": (8, 75)}
PRODUCT_NOUNS = {
    "Apparel": ["Tee", "Hoodie", "Jacket", "Chinos", "Dress", "Sweater", "Shirt", "Jeans", "Shorts", "Coat"],
    "Home": ["Mug Set", "Throw", "Lamp", "Pillow", "Vase", "Towel Set", "Candle", "Frame", "Rug", "Planter"],
    "Accessories": ["Scarf", "Belt", "Cap", "Wallet", "Tote", "Socks", "Gloves", "Sunglasses", "Watch Strap", "Keyring"],
}
ADJECTIVES = ["Classic", "Coastal", "Northern", "Everyday", "Studio", "Harbor", "Trail", "Urban", "Linen", "Heritage"]


@dataclass
class Line:
    order_id: int
    line_number: int
    product_id: int
    quantity: int
    unit_price: int
    discount: int = 0
    refunds: list[tuple[datetime, int, str]] = field(default_factory=list)

    @property
    def gross(self) -> int:
        return self.quantity * self.unit_price

    @property
    def net(self) -> int:
        return self.gross - self.discount


@dataclass
class Order:
    order_id: int
    store_id: int
    customer_id: int | None
    ordered_at: datetime
    status: str
    lines: list[Line] = field(default_factory=list)
    tax: float = 0.0
    shipping: float = 0.0


class Builder:
    def __init__(self) -> None:
        self.rng = random.Random(SEED)
        self.products: list[tuple[int, str, str, int, int]] = []  # id, sku, name, category_id, list_price
        self.by_category: dict[str, list[tuple[int, int]]] = {}  # name -> [(product_id, list_price)]
        self.customers: list[tuple] = []
        self.orders: list[Order] = []
        self.next_order = 100001
        self.next_line = 1

    # -- reference data

    def build_products(self) -> None:
        pid = 1
        for cid, cname in CATEGORIES:
            lo, hi = PRICE_RANGES[cname]
            for adj in ADJECTIVES:
                for noun in PRODUCT_NOUNS[cname]:
                    if self.rng.random() < 0.55:
                        continue
                    price = self.rng.randint(lo, hi)
                    sku = f"{cname[:2].upper()}-{pid:04d}"
                    self.products.append((pid, sku, f"{adj} {noun}", cid, price))
                    self.by_category.setdefault(cname, []).append((pid, price))
                    pid += 1

    def build_customers(self) -> None:
        tiers = ["none"] * 6 + ["silver"] * 3 + ["gold"]
        for cid in range(1, 2201):
            signup = date(2023, 1, 1) + timedelta(days=self.rng.randint(0, 1300))
            home = self.rng.choice([None, 1, 2, 3])
            self.customers.append(
                (cid, f"C{cid:06d}", f"{self.rng.getrandbits(256):064x}", signup, home, self.rng.choice(tiers))
            )

    # -- orders

    def _customer_for(self, store_id: int) -> int | None:
        # Online orders need an account; about 40% of store purchases are guest checkouts.
        if store_id != 1 and self.rng.random() < 0.4:
            return None
        return self.rng.randint(1, len(self.customers))

    def _random_time(self, month: str, earliest_day: int = 1) -> datetime:
        year, mon = map(int, month.split("-"))
        last = (date(year + (mon == 12), mon % 12 + 1, 1) - timedelta(days=1)).day
        day = self.rng.randint(earliest_day, last)
        return datetime(year, mon, day, self.rng.randint(8, 21), self.rng.randint(0, 59), self.rng.randint(0, 59))

    def _new_order(self, store_id: int, when: datetime, status: str = "completed") -> Order:
        order = Order(self.next_order, store_id, self._customer_for(store_id), when, status)
        self.next_order += 1
        self.orders.append(order)
        return order

    def _line_amounts(self, category: str, gross_target: int) -> list[tuple[int, int, int]]:
        """(product_id, quantity, unit_price) lines summing exactly to gross_target."""
        products = self.by_category[category]
        lines: list[tuple[int, int, int]] = []
        remaining = gross_target
        while remaining > 400:
            pid, price = self.rng.choice(products)
            qty = self.rng.choices([1, 2, 3], weights=[70, 22, 8])[0]
            if qty * price >= remaining:
                qty = 1
            lines.append((pid, qty, price))
            remaining -= qty * price
        # Close the gap exactly with a final line at a promotional/odd price.
        while remaining > 0:
            pid, price = self.rng.choice(products)
            unit = min(remaining, max(price, 5))
            lines.append((pid, 1, unit))
            remaining -= unit
        return lines

    def _spread(self, total: int, capacities: list[int], share: float) -> list[int]:
        """Split `total` across a random subset of slots without exceeding each capacity."""
        n = len(capacities)
        chosen = [i for i in range(n) if self.rng.random() < share] or list(range(n))
        weights = [capacities[i] * self.rng.uniform(0.4, 1.0) for i in chosen]
        wsum = sum(weights)
        alloc = [0] * n
        for i, w in zip(chosen, weights):
            alloc[i] = min(capacities[i], int(total * w / wsum))
        left = total - sum(alloc)
        order = sorted(range(n), key=lambda i: capacities[i] - alloc[i], reverse=True)
        k = 0
        while left > 0:
            i = order[k % n]
            room = capacities[i] - alloc[i]
            if room > 0:
                step = min(room, left)
                alloc[i] += step
                left -= step
            k += 1
            if k > 10 * n and left > 0:
                raise RuntimeError("cannot place allocation within capacities")
        return alloc

    def build_q3(self) -> None:
        cells_by_month_outlet: dict[tuple[str, str], list[str]] = {}
        for (month, outlet, category) in TARGETS:
            cells_by_month_outlet.setdefault((month, outlet), []).append(category)

        for (month, outlet), categories in sorted(cells_by_month_outlet.items()):
            store_id = OUTLET_STORE[outlet]
            pending: list[tuple[str, int, int, int]] = []  # category, product, qty, price
            for category in categories:
                for pid, qty, price in self._line_amounts(category, TARGETS[(month, outlet, category)][0]):
                    pending.append((category, pid, qty, price))
            self.rng.shuffle(pending)

            # Pack lines into orders of 1-4 lines; categories mix within an order.
            cell_lines: dict[str, list[Line]] = {c: [] for c in categories}
            i = 0
            while i < len(pending):
                size = self.rng.choices([1, 2, 3, 4], weights=[45, 30, 17, 8])[0]
                order = self._new_order(store_id, self._random_time(month))
                for n, (category, pid, qty, price) in enumerate(pending[i : i + size], start=1):
                    line = Line(order.order_id, n, pid, qty, price)
                    order.lines.append(line)
                    cell_lines[category].append(line)
                i += size

            for category in categories:
                _, discounts, returns = TARGETS[(month, outlet, category)]
                lines = cell_lines[category]
                # Discounts: about 40% of lines, never more than 60% of a line's value.
                for line, d in zip(lines, self._spread(discounts, [int(l.gross * 0.6) for l in lines], 0.4)):
                    line.discount = d
                # Refunds within the snapshot: net of discount, ~12% of lines, some split in two.
                for line, r in zip(lines, self._spread(returns, [l.net for l in lines], 0.12)):
                    if r:
                        self._add_refunds(line, r, cutoff=SNAPSHOT_CUTOFF)

    def _order_of(self, line: Line) -> Order:
        return self._orders_by_id[line.order_id]

    def _add_refunds(self, line: Line, amount: int, cutoff: datetime) -> None:
        ordered = self._order_of(line).ordered_at
        parts = [amount]
        if amount >= 20 and self.rng.random() < 0.25:  # multiple refund events on one line
            first = self.rng.randint(amount // 4, amount * 3 // 4)
            parts = [first, amount - first]
        when = ordered
        for part in parts:
            when = min(when + timedelta(days=self.rng.randint(2, 21), hours=self.rng.randint(0, 9)),
                       cutoff - timedelta(minutes=self.rng.randint(1, 600)))
            when = max(when, ordered + timedelta(hours=1))
            line.refunds.append((when, part, self.rng.choice(["size", "damaged", "changed_mind", "not_as_described"])))

    def build_late_refunds(self) -> int:
        """Discrepancy 1: refunds after the snapshot against Q3 orders (mostly September)."""
        candidates = [
            l for o in self.orders if o.status == "completed" and o.store_id in (1, 2, 3)
            and o.ordered_at >= datetime(2026, 8, 20) and o.ordered_at < datetime(2026, 10, 1)
            for l in o.lines if l.net - sum(r[1] for r in l.refunds) >= 40
        ]
        total = 0
        for line in self.rng.sample(candidates, 18):
            room = line.net - sum(r[1] for r in line.refunds)
            amount = self.rng.randint(25, min(room, 260))
            when = datetime(2026, 10, 8) + timedelta(days=self.rng.randint(0, 22), hours=self.rng.randint(8, 20))
            line.refunds.append((when, amount, self.rng.choice(["size", "damaged", "changed_mind"])))
            total += amount
        return total

    def build_harbor(self) -> int:
        """Discrepancy 2: completed September orders at the undocumented Harbor pilot store."""
        gross = 0
        for _ in range(64):
            order = self._new_order(4, self._random_time("2026-09", earliest_day=15))
            for n in range(1, self.rng.choice([1, 1, 2, 3]) + 1):
                category = self.rng.choice(["Apparel", "Home", "Accessories"])
                pid, price = self.rng.choice(self.by_category[category])
                line = Line(order.order_id, n, pid, self.rng.choice([1, 1, 2]), price)
                if self.rng.random() < 0.3:
                    line.discount = int(line.gross * self.rng.uniform(0.05, 0.25))
                order.lines.append(line)
                gross += line.gross
        return gross

    def build_noise(self) -> None:
        """Orders the reports must exclude: cancellations, and June/October orders outside Q3."""
        for month, count, status in [("2026-06", 220, "completed"), ("2026-10", 260, "completed"),
                                     ("2026-07", 40, "cancelled"), ("2026-08", 45, "cancelled"),
                                     ("2026-09", 48, "cancelled")]:
            for _ in range(count):
                store_id = self.rng.choice([1, 1, 2, 3])
                when = self._random_time(month) if month != "2026-10" else datetime(2026, 10, 1) + timedelta(
                    days=self.rng.randint(0, 29), hours=self.rng.randint(8, 21))
                order = self._new_order(store_id, when, status)
                for n in range(1, self.rng.choice([1, 2, 2, 3]) + 1):
                    category = self.rng.choice(["Apparel", "Home", "Accessories"])
                    pid, price = self.rng.choice(self.by_category[category])
                    order.lines.append(Line(order.order_id, n, pid, self.rng.choice([1, 1, 2]), price))

    def finish_orders(self) -> None:
        for order in self.orders:
            net = sum(l.net for l in order.lines)
            order.tax = round(net * (0.0 if order.store_id == 1 else 0.0825), 2)
            order.shipping = (0.0 if net >= 75 else 6.95) if order.store_id == 1 else 0.0
        self.orders.sort(key=lambda o: (o.ordered_at, o.order_id))
        # Renumber so order ids follow time, like a real sequence.
        for new_id, order in enumerate(self.orders, start=100001):
            order.order_id = new_id
            for line in order.lines:
                line.order_id = new_id

    def costs(self) -> list[tuple]:
        rows = []
        day = date(2026, 6, 1)
        while day <= date(2026, 10, 31):
            weekend = day.weekday() >= 5
            for sid, code, *_ , opened in STORES:
                if day < opened:
                    continue
                if code == "WEB":
                    rows.append((sid, day, "fulfillment", self.rng.randint(380, 620)))
                    rows.append((sid, day, "platform", 145))
                else:
                    base = {"DTN": 1250, "RVS": 980, "HBR": 640}[code]
                    rows.append((sid, day, "labor", int(base * (1.35 if weekend else 1.0) * self.rng.uniform(0.92, 1.08))))
                    rows.append((sid, day, "rent", {"DTN": 410, "RVS": 330, "HBR": 260}[code]))
                    rows.append((sid, day, "utilities", self.rng.randint(55, 95)))
            day += timedelta(days=1)
        return rows

    def build(self) -> dict[str, int]:
        self.build_products()
        self.build_customers()
        self._orders_by_id: dict[int, Order] = {}
        # Wrap order creation so refunds can find their order.
        original = self._new_order

        def tracked(*a, **kw):
            o = original(*a, **kw)
            self._orders_by_id[o.order_id] = o
            return o

        self._new_order = tracked  # type: ignore[method-assign]
        self.build_q3()
        late = self.build_late_refunds()
        harbor = self.build_harbor()
        self.build_noise()
        self.finish_orders()
        return {"late_refunds": late, "harbor_gross": harbor}


# --- SQL output -----------------------------------------------------------------------------

SCHEMA = """
DROP TABLE IF EXISTS refunds, order_lines, orders, customers, products, product_categories,
    store_daily_costs, stores;

CREATE TABLE stores (
  store_id      INT PRIMARY KEY,
  store_code    VARCHAR(8) NOT NULL UNIQUE,
  store_name    VARCHAR(60) NOT NULL,
  channel       ENUM('online','store') NOT NULL,
  status        ENUM('active','pilot','closed') NOT NULL,
  opened_on     DATE NOT NULL
) COMMENT='Sales outlets. The web shop is store_id 1.';

CREATE TABLE product_categories (
  category_id   INT PRIMARY KEY,
  category_name VARCHAR(40) NOT NULL UNIQUE
);

CREATE TABLE products (
  product_id    INT PRIMARY KEY,
  sku           VARCHAR(16) NOT NULL UNIQUE,
  product_name  VARCHAR(80) NOT NULL,
  category_id   INT NOT NULL,
  list_price    DECIMAL(10,2) NOT NULL,
  CONSTRAINT fk_products_category FOREIGN KEY (category_id) REFERENCES product_categories(category_id)
);

CREATE TABLE customers (
  customer_id     INT PRIMARY KEY,
  customer_number VARCHAR(12) NOT NULL UNIQUE,
  email_hash      CHAR(64) NOT NULL COMMENT 'SHA-256 of the lowercased email; raw emails are not stored here',
  signup_date     DATE NOT NULL,
  home_store_id   INT NULL,
  loyalty_tier    ENUM('none','silver','gold') NOT NULL,
  CONSTRAINT fk_customers_store FOREIGN KEY (home_store_id) REFERENCES stores(store_id)
);

CREATE TABLE orders (
  order_id        BIGINT PRIMARY KEY,
  order_number    VARCHAR(16) NOT NULL UNIQUE,
  store_id        INT NOT NULL,
  customer_id     INT NULL COMMENT 'NULL for in-store guest checkout',
  ordered_at      DATETIME NOT NULL COMMENT 'UTC',
  status          ENUM('completed','cancelled') NOT NULL,
  tax_amount      DECIMAL(10,2) NOT NULL,
  shipping_amount DECIMAL(10,2) NOT NULL,
  CONSTRAINT fk_orders_store FOREIGN KEY (store_id) REFERENCES stores(store_id),
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
  INDEX ix_orders_ordered_at (ordered_at)
);

CREATE TABLE order_lines (
  order_line_id   BIGINT PRIMARY KEY,
  order_id        BIGINT NOT NULL,
  line_number     INT NOT NULL,
  product_id      INT NOT NULL,
  quantity        INT NOT NULL,
  unit_price      DECIMAL(10,2) NOT NULL,
  discount_amount DECIMAL(10,2) NOT NULL DEFAULT 0,
  UNIQUE KEY uq_order_lines (order_id, line_number),
  CONSTRAINT fk_lines_order FOREIGN KEY (order_id) REFERENCES orders(order_id),
  CONSTRAINT fk_lines_product FOREIGN KEY (product_id) REFERENCES products(product_id)
);

CREATE TABLE refunds (
  refund_id       BIGINT PRIMARY KEY,
  order_line_id   BIGINT NOT NULL,
  refunded_at     DATETIME NOT NULL COMMENT 'UTC',
  refund_amount   DECIMAL(10,2) NOT NULL COMMENT 'Merchandise refund, net of the original line discount',
  reason_code     VARCHAR(24) NOT NULL,
  CONSTRAINT fk_refunds_line FOREIGN KEY (order_line_id) REFERENCES order_lines(order_line_id),
  INDEX ix_refunds_refunded_at (refunded_at)
);

CREATE TABLE store_daily_costs (
  store_id   INT NOT NULL,
  cost_date  DATE NOT NULL,
  cost_type  ENUM('labor','rent','utilities','fulfillment','platform') NOT NULL,
  amount     DECIMAL(10,2) NOT NULL,
  PRIMARY KEY (store_id, cost_date, cost_type),
  CONSTRAINT fk_costs_store FOREIGN KEY (store_id) REFERENCES stores(store_id)
) COMMENT='Operating costs per store per day. No order-level costs are recorded.';
"""


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


def render(builder: Builder) -> str:
    lines, refunds = [], []
    line_id, refund_id = 1, 1
    for order in builder.orders:
        for line in order.lines:
            lines.append((line_id, order.order_id, line.line_number, line.product_id, line.quantity,
                          line.unit_price, line.discount))
            for when, amount, reason in sorted(line.refunds):
                refunds.append((refund_id, line_id, when, amount, reason))
                refund_id += 1
            line_id += 1
    parts = [
        "SET NAMES utf8mb4;",
        SCHEMA,
        inserts("stores", ["store_id", "store_code", "store_name", "channel", "status", "opened_on"], STORES),
        inserts("product_categories", ["category_id", "category_name"], CATEGORIES),
        inserts("products", ["product_id", "sku", "product_name", "category_id", "list_price"], builder.products),
        inserts("customers", ["customer_id", "customer_number", "email_hash", "signup_date", "home_store_id",
                              "loyalty_tier"], builder.customers),
        inserts("orders", ["order_id", "order_number", "store_id", "customer_id", "ordered_at", "status",
                           "tax_amount", "shipping_amount"],
                [(o.order_id, f"NS-{o.order_id}", o.store_id, o.customer_id, o.ordered_at, o.status, o.tax,
                  o.shipping) for o in builder.orders]),
        inserts("order_lines", ["order_line_id", "order_id", "line_number", "product_id", "quantity",
                                "unit_price", "discount_amount"], lines),
        inserts("refunds", ["refund_id", "order_line_id", "refunded_at", "refund_amount", "reason_code"], refunds),
        inserts("store_daily_costs", ["store_id", "cost_date", "cost_type", "amount"], builder.costs()),
    ]
    return "\n\n".join(parts) + "\n"


if __name__ == "__main__":
    builder = Builder()
    facts = builder.build()
    sys.stdout.write(render(builder))
    print(f"-- generated {len(builder.orders)} orders; late refunds {facts['late_refunds']}, "
          f"harbor gross {facts['harbor_gross']}", file=sys.stderr)
