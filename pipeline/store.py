"""Persist each reconciliation run into a SQLite database, so results can be consumed
later (queries, trends, a dashboard). Portable file; lives on the VPS in production.

Ingests the two CSVs (archivos_*.csv + comprobantes.csv) as one run snapshot. History
accumulates: every run gets a run_id + timestamp; nothing is overwritten. Query the
latest with the `v_latest_*` views.

Usage:
    python store.py --archivos out/archivos_60d.csv --comprobantes out/comprobantes.csv \
                    --db out/edenia.db [--days 60]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, ts TEXT, days INTEGER, total INTEGER
);
CREATE TABLE IF NOT EXISTS reservations (
    run_id TEXT, cid TEXT, checkout TEXT, guest TEXT, channel TEXT, api_verdict TEXT,
    total REAL, paid REAL, unpaid REAL, currency TEXT,
    has_receipt INTEGER, receipt_amount REAL, receipt_currency TEXT, receipt_date TEXT,
    expected TEXT, fx_date TEXT, fx_rate REAL, match_flag TEXT, match_detail TEXT,
    bulk_size INTEGER, bulk_cids TEXT, receipt_hash TEXT,
    PRIMARY KEY (run_id, cid)
);
CREATE TABLE IF NOT EXISTS files (
    run_id TEXT, cid TEXT, file TEXT, kind TEXT, is_receipt INTEGER, signal TEXT,
    amount REAL, currency TEXT, date TEXT, how TEXT, hash TEXT
);
CREATE INDEX IF NOT EXISTS ix_res_cid ON reservations(cid);
CREATE INDEX IF NOT EXISTS ix_res_flag ON reservations(run_id, match_flag);
CREATE INDEX IF NOT EXISTS ix_files_hash ON files(hash);
-- latest run helpers
CREATE VIEW IF NOT EXISTS v_last_run AS SELECT run_id FROM runs ORDER BY ts DESC LIMIT 1;
CREATE VIEW IF NOT EXISTS v_latest_reservations AS
    SELECT r.* FROM reservations r JOIN v_last_run l ON r.run_id = l.run_id;
CREATE VIEW IF NOT EXISTS v_latest_files AS
    SELECT f.* FROM files f JOIN v_last_run l ON f.run_id = l.run_id;
"""


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archivos", required=True)
    ap.add_argument("--comprobantes", required=True)
    ap.add_argument("--db", default="out/edenia.db")
    ap.add_argument("--days", type=int, default=0)
    args = ap.parse_args()

    arch = {r["cid"]: r for r in csv.DictReader(open(args.archivos, encoding="utf-8"))}
    comp = {r["cid"]: r for r in csv.DictReader(open(args.comprobantes, encoding="utf-8"))}
    files = list(csv.DictReader(open(args.comprobantes.replace(".csv", "_files.csv"), encoding="utf-8"))) \
        if Path(args.comprobantes.replace(".csv", "_files.csv")).exists() else []

    run_id = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    ts = dt.datetime.now().isoformat(timespec="seconds")

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(args.db)
    con.executescript(SCHEMA)
    con.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?)", (run_id, ts, args.days, len(arch)))

    for cid, a in arch.items():
        c = comp.get(cid, {})
        con.execute(
            "INSERT OR REPLACE INTO reservations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, cid, a.get("checkout"), a.get("guest"), a.get("channel"), a.get("api_verdict"),
             _num(a.get("total")), _num(a.get("paid")), _num(a.get("unpaid")), a.get("currency"),
             1 if c.get("has_receipt") == "True" else 0, _num(c.get("receipt_amount")),
             c.get("receipt_currency"), c.get("receipt_date"), c.get("expected"),
             c.get("fx_date"), _num(c.get("fx_rate")), c.get("match_flag"), c.get("match_detail"),
             _int(c.get("bulk_size")), c.get("bulk_cids"), c.get("receipt_hash")))
    for f in files:
        con.execute(
            "INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, f.get("cid"), f.get("file"), f.get("kind"),
             1 if f.get("is_receipt") == "True" else 0, f.get("signal"), _num(f.get("amount")),
             f.get("currency"), f.get("date"), f.get("how"), f.get("hash")))
    con.commit()

    n_res = con.execute("SELECT COUNT(*) FROM reservations WHERE run_id=?", (run_id,)).fetchone()[0]
    n_files = con.execute("SELECT COUNT(*) FROM files WHERE run_id=?", (run_id,)).fetchone()[0]
    n_runs = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    con.close()
    print(f"stored run {run_id}: {n_res} reservations, {n_files} files. DB {args.db} now holds {n_runs} run(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
