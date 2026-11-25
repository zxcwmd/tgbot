CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    file TEXT NOT NULL,
    name TEXT NOT NULL,
    price INTEGER NOT NULL CHECK (price BETWEEN 1 AND 10000),
    active INTEGER NOT NULL DEFAULT 1,
    source INTEGER NOT NULL UNIQUE
);
CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    buyer INTEGER NOT NULL,
    file TEXT NOT NULL,
    price INTEGER NOT NULL,
    charge TEXT UNIQUE,
    delivered INTEGER NOT NULL DEFAULT 0,
    retry_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX pending_orders ON orders(retry_at) WHERE charge IS NOT NULL AND delivered = 0;
