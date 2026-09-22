-- 造数与回归用表结构（示例，替换为你自己的库结构即可）
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS dept (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  dept_no  TEXT NOT NULL UNIQUE,
  name     TEXT NOT NULL,
  status   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS users (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  username   TEXT NOT NULL UNIQUE,
  email      TEXT NOT NULL UNIQUE,
  phone      TEXT,
  age        INTEGER,
  role       TEXT NOT NULL DEFAULT 'user',
  dept       TEXT,
  vip        INTEGER NOT NULL DEFAULT 0,
  status     INTEGER NOT NULL DEFAULT 1,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS orders (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no   TEXT NOT NULL UNIQUE,
  user_id    INTEGER NOT NULL,
  amount     REAL NOT NULL,
  currency   TEXT NOT NULL DEFAULT 'CNY',
  status     TEXT NOT NULL DEFAULT 'created',
  created_at TEXT,
  FOREIGN KEY (user_id) REFERENCES users(id)
);
