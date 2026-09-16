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

import sys as _sys
try:
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

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
    pagos_n INTEGER, pagos_metodos TEXT, pagos_cobrado TEXT, pagos_ultimo TEXT,
    has_receipt INTEGER, receipt_amount REAL, receipt_currency TEXT, receipt_date TEXT,
    expected TEXT, fx_date TEXT, fx_rate REAL, match_flag TEXT, match_detail TEXT,
    bulk_size INTEGER, bulk_cids TEXT, receipt_hash TEXT,
    PRIMARY KEY (run_id, cid)
);
CREATE TABLE IF NOT EXISTS pagos (
    run_id TEXT, cid TEXT, fecha TEXT, metodo TEXT, concepto TEXT, moneda TEXT,
    ingreso REAL, egreso REAL, usd REAL, usuario TEXT
);
CREATE INDEX IF NOT EXISTS ix_pagos_cid ON pagos(cid);
CREATE VIEW IF NOT EXISTS v_latest_pagos AS
    SELECT p.* FROM pagos p JOIN v_last_run l ON p.run_id = l.run_id;
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
    pagos_path = Path(args.archivos).parent / "pagos.csv"
    pagos = list(csv.DictReader(open(pagos_path, encoding="utf-8"))) if pagos_path.exists() else []

    run_id = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    ts = dt.datetime.now().isoformat(timespec="seconds")

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(args.db)
    con.executescript(SCHEMA)
    con.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?)", (run_id, ts, args.days, len(arch)))

    def insert(table, row):  # column-named insert = order-independent, migration-safe
        cols = ",".join(row.keys())
        con.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({','.join('?'*len(row))})",
                    tuple(row.values()))

    for cid, a in arch.items():
        c = comp.get(cid, {})
        insert("reservations", {
            "run_id": run_id, "cid": cid, "checkout": a.get("checkout"), "guest": a.get("guest"),
            "channel": a.get("channel"), "api_verdict": a.get("api_verdict"),
            "total": _num(a.get("total")), "paid": _num(a.get("paid")), "unpaid": _num(a.get("unpaid")),
            "currency": a.get("currency"), "pagos_n": _int(a.get("pagos_n")),
            "pagos_metodos": a.get("pagos_metodos"), "pagos_cobrado": a.get("pagos_cobrado"),
            "pagos_ultimo": a.get("pagos_ultimo"),
            "has_receipt": 1 if c.get("has_receipt") == "True" else 0,
            "receipt_amount": _num(c.get("receipt_amount")), "receipt_currency": c.get("receipt_currency"),
            "receipt_date": c.get("receipt_date"), "expected": c.get("expected"),
            "fx_date": c.get("fx_date"), "fx_rate": _num(c.get("fx_rate")),
            "match_flag": c.get("match_flag"), "match_detail": c.get("match_detail"),
            "bulk_size": _int(c.get("bulk_size")), "bulk_cids": c.get("bulk_cids"),
            "receipt_hash": c.get("receipt_hash")})
    for f in files:
        insert("files", {
            "run_id": run_id, "cid": f.get("cid"), "file": f.get("file"), "kind": f.get("kind"),
            "is_receipt": 1 if f.get("is_receipt") == "True" else 0, "signal": f.get("signal"),
            "amount": _num(f.get("amount")), "currency": f.get("currency"), "date": f.get("date"),
            "how": f.get("how"), "hash": f.get("hash")})
    for p in pagos:
        insert("pagos", {
            "run_id": run_id, "cid": p.get("cid"), "fecha": p.get("fecha"), "metodo": p.get("metodo"),
            "concepto": p.get("concepto"), "moneda": p.get("moneda"), "ingreso": _num(p.get("ingreso")),
            "egreso": _num(p.get("egreso")), "usd": _num(p.get("usd")), "usuario": p.get("usuario")})
    con.commit()

    n_res = con.execute("SELECT COUNT(*) FROM reservations WHERE run_id=?", (run_id,)).fetchone()[0]
    n_files = con.execute("SELECT COUNT(*) FROM files WHERE run_id=?", (run_id,)).fetchone()[0]
    n_runs = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    con.close()
    print(f"stored run {run_id}: {n_res} reservations, {n_files} files. DB {args.db} now holds {n_runs} run(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
