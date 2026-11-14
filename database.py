"""Data layer for the lightweight POS / ticketing app.

Handles items, sales, transactions and receipts using SQLite.
The database path comes from the DB_PATH environment variable so the
same code runs on the host and inside a Docker container (volume).
"""
import os
import sqlite3
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "pos.db")

SAMPLE_ITEMS = [
    # (name, price, category)  - the first six keep their original ids
    ("Bread", 1.50, "Bakery"),
    ("Milk", 2.00, "Dairy & Eggs"),
    ("Soda", 1.00, "Drinks"),
    ("Eggs (tray)", 4.50, "Dairy & Eggs"),
    ("Rice (2kg)", 3.75, "Pantry"),
    ("Cooking oil (1L)", 3.20, "Pantry"),
    ("Buns (6)", 1.80, "Bakery"),
    ("Scones (4)", 2.40, "Bakery"),
    ("Rusks", 2.20, "Bakery"),
    ("Yoghurt", 1.20, "Dairy & Eggs"),
    ("Butter", 2.60, "Dairy & Eggs"),
    ("Cheese block", 3.90, "Dairy & Eggs"),
    ("Mineral water", 0.80, "Drinks"),
    ("Orange juice (1L)", 2.50, "Drinks"),
    ("Tea bags (50)", 2.10, "Drinks"),
    ("Coffee (200g)", 5.40, "Drinks"),
    ("Sugar (2kg)", 3.10, "Pantry"),
    ("Maize meal (5kg)", 4.80, "Pantry"),
    ("Salt", 0.70, "Pantry"),
    ("Pasta", 1.60, "Pantry"),
    ("Baked beans", 1.30, "Pantry"),
    ("Tomatoes (1kg)", 1.90, "Fresh produce"),
    ("Onions (1kg)", 1.40, "Fresh produce"),
    ("Bananas (1kg)", 1.60, "Fresh produce"),
    ("Potatoes (2kg)", 2.30, "Fresh produce"),
    ("Cabbage", 1.10, "Fresh produce"),
    ("Washing powder", 4.20, "Household"),
    ("Dish soap", 2.10, "Household"),
    ("Toilet paper (4)", 3.00, "Household"),
    ("Matches", 0.40, "Household"),
    ("Chips", 1.10, "Snacks"),
    ("Biscuits", 1.35, "Snacks"),
    ("Chocolate bar", 1.25, "Snacks"),
    ("Peanuts (200g)", 1.70, "Snacks"),
]


class SaleError(ValueError):
    """Raised when a sale request is invalid (bad item, bad quantity, empty cart)."""


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if needed and add sample items on first run."""
    conn = get_connection()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            category TEXT NOT NULL DEFAULT 'General'
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            total REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transaction_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL,
            FOREIGN KEY (transaction_id) REFERENCES transactions(id),
            FOREIGN KEY (item_id) REFERENCES items(id)
        );
        """
    )
    # Upgrade databases created before categories existed
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(items)")]
    if "category" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN category TEXT NOT NULL DEFAULT 'General'")
    # Add any sample items that are missing (matched by name).
    # Existing items keep their prices, so old receipts stay correct.
    existing = {r["name"] for r in conn.execute("SELECT name FROM items")}
    for name, price, category in SAMPLE_ITEMS:
        if name in existing:
            conn.execute(
                "UPDATE items SET category = ? WHERE name = ? AND category = 'General'",
                (category, name),
            )
        else:
            conn.execute(
                "INSERT INTO items (name, price, category) VALUES (?, ?, ?)",
                (name, price, category),
            )
    conn.commit()
    conn.close()


# ---------- Items ----------

def get_items():
    conn = get_connection()
    rows = conn.execute("SELECT id, name, price, category FROM items ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- Selling ----------

def create_sale(cart):
    """Save a sale. cart = [{"item_id": 1, "quantity": 2}, ...].

    Returns the full transaction (including lines and total).
    Raises SaleError for an empty cart, unknown item or bad quantity.
    """
    if not isinstance(cart, list) or len(cart) == 0:
        raise SaleError("The cart is empty.")

    conn = get_connection()
    try:
        lines = []
        total = 0.0
        for line in cart:
            try:
                item_id = int(line["item_id"])
                quantity = int(line["quantity"])
            except (KeyError, TypeError, ValueError):
                raise SaleError("Each cart line needs a numeric item_id and quantity.")
            if quantity <= 0:
                raise SaleError("Quantity must be at least 1.")

            item = conn.execute(
                "SELECT id, price FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if item is None:
                raise SaleError(f"Item {item_id} does not exist.")

            lines.append((item["id"], quantity, item["price"]))
            total += item["price"] * quantity

        cur = conn.execute(
            "INSERT INTO transactions (created_at, total) VALUES (?, ?)",
            (datetime.now().isoformat(timespec="seconds"), round(total, 2)),
        )
        transaction_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO transaction_items (transaction_id, item_id, quantity, unit_price) "
            "VALUES (?, ?, ?, ?)",
            [(transaction_id, i, q, p) for i, q, p in lines],
        )
        conn.commit()
    finally:
        conn.close()

    return get_transaction(transaction_id)


# ---------- Retrieving transactions ----------

def get_transaction(transaction_id):
    """Return one transaction with its lines, or None if it does not exist."""
    conn = get_connection()
    trans = conn.execute(
        "SELECT id, created_at, total FROM transactions WHERE id = ?",
        (transaction_id,),
    ).fetchone()
    if trans is None:
        conn.close()
        return None
    rows = conn.execute(
        """SELECT i.name, ti.unit_price AS price, ti.quantity
           FROM transaction_items ti JOIN items i ON i.id = ti.item_id
           WHERE ti.transaction_id = ?
           ORDER BY ti.id""",
        (transaction_id,),
    ).fetchall()
    conn.close()

    result = dict(trans)
    result["lines"] = [
        {**dict(r), "line_total": round(r["price"] * r["quantity"], 2)} for r in rows
    ]
    return result


def list_transactions(limit=20):
    """Most recent transactions first (summary only)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, created_at, total FROM transactions ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- Receipts ----------

def format_receipt(transaction, width=38):
    """Build a plain-text receipt for a transaction dict."""
    bar = "=" * width
    out = [
        bar,
        "LIGHTWEIGHT POS RECEIPT".center(width),
        bar,
        f"Receipt no: {transaction['id']}",
        f"Date:       {transaction['created_at'].replace('T', ' ')}",
        "-" * width,
    ]
    for ln in transaction["lines"]:
        out.append(ln["name"])
        out.append(
            f"  {ln['quantity']} x {ln['price']:.2f}".ljust(width - 10)
            + f"{ln['line_total']:>10.2f}"
        )
    out += [
        "-" * width,
        "TOTAL".ljust(width - 10) + f"{transaction['total']:>10.2f}",
        bar,
        "Thank you for your purchase".center(width),
    ]
    return "\n".join(out)


if __name__ == "__main__":
    # Quick self-test:  python database.py
    init_db()
    print("Items:", get_items())
    sale = create_sale([{"item_id": 1, "quantity": 2}, {"item_id": 2, "quantity": 1}])
    print(format_receipt(sale))
    print("Recent:", list_transactions(5))
