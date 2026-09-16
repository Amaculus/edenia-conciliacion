"""Turn the two reconciliation CSVs into a prioritized, human-readable findings report.

Merges the presence check (archivos_*.csv: API money status per check-out) with the
receipt reading (comprobantes.csv: receipt found + amount reconciliation) and groups
every reservation into an action bucket, most urgent first.

Usage:
    python report.py --archivos out/archivos_60d.csv --comprobantes out/comprobantes.csv [--out out/hallazgos.txt]
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

PMS = "https://pms.pxsol.com/bookings/cid.html?CID={cid}"
COLLECTED = "COBRADO"                       # API says paid
UNCOLLECTED = {"A COBRAR", "PARCIAL", "SIN COMPROBANTES"}  # API says not (fully) paid


def load(path: str) -> dict[str, dict]:
    return {r["cid"]: r for r in csv.DictReader(open(path, encoding="utf-8"))}


def money(amt, cur: str = "") -> str:
    if amt in (None, "", "None"):
        return ""
    try:
        s = f"{float(amt):,.2f}"
    except (TypeError, ValueError):
        s = str(amt)
    return f"{cur} {s}".strip() if cur else s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archivos", required=True)
    ap.add_argument("--comprobantes", required=True)
    ap.add_argument("--out")
    ap.add_argument("--html")
    ap.add_argument("--artifact-html", help="body-only HTML for a Claude Artifact (no doctype/head wrappers)")
    args = ap.parse_args()

    import datetime
    today = datetime.date.today()
    arch = load(args.archivos)
    comp = load(args.comprobantes)

    def is_ota(ch: str) -> bool:
        ch = ch.lower()
        return "ota" in ch or any(x in ch for x in
            ("booking", "despegar", "expedia", "cvc", "hyperguest", "juniper", "amichi", "hotelbeds"))

    def days_since(d: str) -> int:
        try:
            return (today - datetime.date.fromisoformat(d[:10])).days
        except Exception:  # noqa: BLE001
            return -1

    buckets: dict[str, list] = {k: [] for k in
        ("cobro_no_registrado", "chase_directo_agencia", "monto_no_coincide",
         "comprobante_ilegible", "chase_ota", "cobrado_sin_respaldo", "conciliado")}
    bulk: dict[str, list] = {}

    for cid, a in arch.items():
        api = a.get("api_verdict", "")
        ch = a.get("channel", "")
        c = comp.get(cid)
        has_receipt = bool(c and c.get("has_receipt") == "True")
        mflag = c.get("match_flag") if c else None
        dias = days_since(a.get("checkout", ""))
        # Expected: authoritative reservation total (summary endpoint, present for every
        # reservation), falling back to the receipt-reader's expected.
        expected = money(a.get("total"), a.get("currency")) or (c or {}).get("expected", "")
        try:
            total_num = float(a.get("total"))
        except (TypeError, ValueError):
            total_num = None
        rec = {"cid": cid, "guest": a.get("guest", ""), "channel": ch,
               "checkout": a.get("checkout", ""), "dias": dias, "api": api, "match_flag": mflag,
               "receipt": money((c or {}).get("receipt_amount", ""), (c or {}).get("receipt_currency", "")),
               "expected": expected, "_total": total_num, "_currency": (a.get("currency") or "").strip(),
               "detail": (c or {}).get("match_detail", ""), "url": PMS.format(cid=cid)}
        # A real lote = reservations that literally share the same receipt file (same hash).
        # Bulk members live ONLY in the lote section, never in the per-reservation buckets.
        if c and c.get("bulk_size") not in ("", "1", None) and c.get("receipt_hash"):
            bulk.setdefault(c["receipt_hash"], []).append(rec)
            continue

        collected = api == COLLECTED
        if not collected and has_receipt and mflag in ("OK-MONTO", "OK-BULK"):
            buckets["cobro_no_registrado"].append(rec)          # money in (valid receipt) but API unpaid
        elif not collected and not has_receipt and api in UNCOLLECTED:
            # Honest split: OTA = the OTA's own collection flow (José); everything else = unpaid.
            buckets["chase_ota" if is_ota(ch) else "chase_directo_agencia"].append(rec)
        elif has_receipt and mflag == "REVISAR-MONTO":
            buckets["monto_no_coincide"].append(rec)            # receipt vs expected mismatch
        elif has_receipt and mflag == "MONTO-ILEGIBLE":
            buckets["comprobante_ilegible"].append(rec)         # couldn't read amount
        elif collected and not has_receipt:
            buckets["cobrado_sin_respaldo"].append(rec)         # paid (usually OTA) no uploaded proof
        else:
            buckets["conciliado"].append(rec)

    lines: list[str] = []
    def out(s=""): lines.append(s)

    total = len(arch)
    out(f"EDENIA - CONCILIACION DE COBROS ({total} check-outs)")
    out("=" * 60)
    order = [
        ("cobro_no_registrado", "1) COBRO NO REGISTRADO (hay comprobante, el sistema figura impago)"),
        ("chase_directo_agencia", "2) SIN COBRAR - DIRECTO/AGENCIA (impagas, perseguir cobro)"),
        ("monto_no_coincide", "3) COMPROBANTE NO COINCIDE CON LO ESPERADO (revisar)"),
        ("comprobante_ilegible", "4) COMPROBANTE ILEGIBLE (revisar a mano)"),
        ("chase_ota", "5) SIN COBRAR - OTA (flujo de la OTA / Jose, verificar)"),
        ("cobrado_sin_respaldo", "6) COBRADO SIN COMPROBANTE SUBIDO (mayormente OTAs, informativo)"),
        ("conciliado", "7) CONCILIADO (sin accion)"),
    ]
    def line(r, ind="  "):
        extra = f"  [{r['detail']}]" if r["detail"] else ""
        dias = f"{r['dias']}d" if r["dias"] >= 0 else "?"
        out(f"{ind}{r['checkout']} (+{dias}) {r['cid']} {r['guest'][:24]:24} [{r['channel']}] "
            f"esperado={r['expected'] or '-'} recibo={r['receipt'] or '-'}{extra}")
        out(f"{ind}     {r['url']}")

    for key, title in order:
        rows = buckets[key]
        out(f"\n{title}: {len(rows)}")
        for r in sorted(rows, key=lambda x: x["dias"], reverse=True):
            line(r)

    shared = {h: v for h, v in bulk.items() if len({r['cid'] for r in v}) > 1}
    if shared:
        n_rev = sum(1 for v in shared.values() if v[0].get("match_flag") != "OK-BULK")
        out(f"\nTRANSFERENCIAS EN LOTE (un comprobante cubre varias reservas): {len(shared)} "
            f"({n_rev} a revisar)")
        for h, v in shared.items():
            ok = v[0].get("match_flag") == "OK-BULK"
            status = "OK cubre" if ok else "REVISAR no cubre"
            out(f"  [{status}] Lote {h}: recibo={v[0]['receipt'] or '-'} vs esperado(suma)={lote_suma(v)} — {len(v)} reservas")
            for r in sorted(v, key=lambda x: x["checkout"]):
                out(f"      {r['cid']} {r['guest'][:24]:24} {r['url']}")

    text = "\n".join(lines)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.html:
        Path(args.html).write_text(render_html(buckets, shared, total, order), encoding="utf-8")
        print(f"HTML: {args.html}")
    if args.artifact_html:
        Path(args.artifact_html).write_text(render_html(buckets, shared, total, order, artifact=True), encoding="utf-8")
        print(f"Artifact HTML: {args.artifact_html}")
    return 0


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def lote_suma(members: list[dict]) -> str:
    """Sum the members' reservation totals per currency for the lote coverage line."""
    by = {}
    for r in members:
        if r.get("_total") is not None and r.get("_currency"):
            by[r["_currency"]] = by.get(r["_currency"], 0) + r["_total"]
    return ", ".join(money(a, c) for c, a in by.items()) or "—"


PRIORITY = {"cobro_no_registrado": "hi", "chase_directo_agencia": "hi",
            "monto_no_coincide": "mid", "comprobante_ilegible": "mid",
            "chase_ota": "low", "cobrado_sin_respaldo": "low", "conciliado": "ok"}


def render_html(buckets, shared, total, order, artifact: bool = False) -> str:
    import datetime
    font = ('<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
            'family=Fraunces:opsz,wght@9..144,500;9..144,600&family=IBM+Plex+Sans:wght@400;500;600&'
            'family=IBM+Plex+Mono:wght@500&display=swap">')
    css = """
    :root{
      --bg:#f4f6f2; --surface:#ffffff; --surface2:#f7f9f5; --ink:#1a201c; --muted:#5d6b61;
      --line:#e2e8e0; --accent:#1f6f4f; --link:#1f6f4f;
      --hi:#b42318; --mid:#b25e09; --low:#5b6b7a; --ok:#1f7a53;
      --hi-bg:#fdeceb; --mid-bg:#fbf1e4; --ok-bg:#e9f4ee; --low-bg:#eef1f4;
    }
    @media (prefers-color-scheme:dark){:root:not([data-theme=light]){
      --bg:#0f1411; --surface:#151b17; --surface2:#1a221d; --ink:#e8ede9; --muted:#9aa89f;
      --line:#26302a; --accent:#57b48c; --link:#7fc7a6;
      --hi:#f0776b; --mid:#e0a45e; --low:#9fb0be; --ok:#67c79b;
      --hi-bg:#2a1614; --mid-bg:#2a2113; --ok-bg:#122a1f; --low-bg:#1a222b;
    }}
    [data-theme=dark]{
      --bg:#0f1411; --surface:#151b17; --surface2:#1a221d; --ink:#e8ede9; --muted:#9aa89f;
      --line:#26302a; --accent:#57b48c; --link:#7fc7a6;
      --hi:#f0776b; --mid:#e0a45e; --low:#9fb0be; --ok:#67c79b;
      --hi-bg:#2a1614; --mid-bg:#2a2113; --ok-bg:#122a1f; --low-bg:#1a222b;
    }
    *{box-sizing:border-box}
    body{font-family:'IBM Plex Sans',system-ui,sans-serif;font-size:14px;line-height:1.5;
      margin:0;background:var(--bg);color:var(--ink)}
    header{border-bottom:2px solid var(--accent);padding:22px 24px;background:var(--surface)}
    header h1{margin:0;font-family:'Fraunces',Georgia,serif;font-weight:600;font-size:24px;letter-spacing:-.01em}
    header .sub{color:var(--muted);font-size:13px;margin-top:4px}
    .wrap{max-width:1120px;margin:0 auto;padding:20px 24px 48px}
    .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:10px;margin:0 0 22px}
    a.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:12px 14px;
      text-decoration:none;color:inherit;display:block;transition:border-color .15s,transform .05s}
    a.card:hover{border-color:var(--accent);transform:translateY(-1px)}
    .card .n{font-family:'IBM Plex Mono',monospace;font-size:26px;font-weight:500;font-variant-numeric:tabular-nums}
    .card .l{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-top:2px}
    .card.hi .n{color:var(--hi)} .card.mid .n{color:var(--mid)} .card.ok .n{color:var(--ok)}
    details{background:var(--surface);border:1px solid var(--line);border-radius:12px;margin:10px 0;overflow:hidden}
    summary{cursor:pointer;padding:13px 16px;font-weight:600;list-style:none;display:flex;
      justify-content:space-between;align-items:center;gap:12px}
    summary::-webkit-details-marker{display:none}
    summary:hover{background:var(--surface2)}
    .badge{font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:500;border-radius:999px;
      padding:2px 11px;color:#fff;flex:none}
    .badge.hi{background:var(--hi)} .badge.mid{background:var(--mid)}
    .badge.low{background:var(--low)} .badge.ok{background:var(--ok)}
    .tw{overflow-x:auto}
    table{width:100%;border-collapse:collapse;font-size:13px}
    td,th{padding:9px 14px;border-top:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}
    td.det{white-space:normal;min-width:200px}
    th{background:var(--surface2);color:var(--muted);font-weight:600;font-size:11px;
      text-transform:uppercase;letter-spacing:.05em;position:sticky;top:0}
    tbody tr:hover{background:var(--surface2)}
    .cid a{color:var(--link);text-decoration:none;font-family:'IBM Plex Mono',monospace}
    .cid a:hover{text-decoration:underline}
    .amt{text-align:right;font-family:'IBM Plex Mono',monospace;font-variant-numeric:tabular-nums}
    .muted{color:var(--muted)} .det{color:var(--muted);font-size:12px}
    h3{font-family:'Fraunces',Georgia,serif;font-weight:600;font-size:17px;margin:26px 0 8px}
    details.sub{margin:8px 12px;border:1px solid var(--line);border-radius:9px}
    details.sub>summary{padding:9px 14px;font-weight:500;background:var(--surface2);font-size:13px}
    a{color:var(--link)}
    """
    head = f'{font}<title>Conciliación Edenia</title><style>{css}</style>' if artifact \
        else f'<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">{font}<title>Conciliación Edenia</title><style>{css}</style>'
    h = [head,
         "<header><h1>Edenia — Conciliación de cobros</h1>"
         f"<div class=sub>{total} check-outs · generado {datetime.datetime.now():%Y-%m-%d %H:%M}</div></header>",
         "<div class=wrap><div class=cards>"]
    labels = {"cobro_no_registrado": "Cobro no registrado", "chase_directo_agencia": "Sin cobrar (Dir./Ag.)",
              "monto_no_coincide": "Monto no coincide", "comprobante_ilegible": "Ilegible",
              "chase_ota": "Sin cobrar (OTA)", "cobrado_sin_respaldo": "Cobrado sin respaldo",
              "conciliado": "Conciliado"}
    for key, _ in order:
        pr = PRIORITY[key]
        cls = f"card {pr}" if pr in ("hi", "mid", "ok") else "card"
        h.append(f"<a class='{cls}' href='#sec-{key}'><div class=n>{len(buckets[key])}</div>"
                 f"<div class=l>{labels[key]}</div></a>")
    h.append("</div>")
    for key, title in order:
        rows = buckets[key]; pr = PRIORITY[key]
        h.append(f"<details id='sec-{key}' {'open' if pr in ('hi','mid') and rows else ''}>"
                 f"<summary><span>{_esc(title)}</span>"
                 f"<span class='badge {pr}'>{len(rows)}</span></summary>")

        def _table(rs):
            t = ["<div class=tw><table><tr><th>Check-out</th><th class=amt>Días</th><th>Reserva</th>"
                 "<th>Huésped</th><th>Canal</th>"
                 "<th class=amt>Esperado</th><th class=amt>Recibo</th><th>Detalle</th></tr>"]
            for r in sorted(rs, key=lambda x: x["dias"], reverse=True):
                dias = f"+{r['dias']}" if r["dias"] >= 0 else "?"
                t.append(f"<tr><td>{_esc(r['checkout'])}</td><td class=amt>{dias}</td>"
                         f"<td class=cid><a href='{r['url']}' target=_blank>{r['cid']}</a></td>"
                         f"<td>{_esc(r['guest'][:30])}</td><td>{_esc(r['channel'])}</td>"
                         f"<td class=amt>{_esc(r['expected'] or '—')}</td>"
                         f"<td class=amt>{_esc(r['receipt'] or '—')}</td>"
                         f"<td class=det>{_esc(r['detail'])}</td></tr>")
            t.append("</table></div>")
            return "".join(t)

        h.append(_table(rows) + "</details>")

    if shared:
        n_rev = sum(1 for v in shared.values() if v[0].get("match_flag") != "OK-BULK")
        h.append(f"<h3 id='sec-lotes' style='margin:22px 0 6px'>Transferencias en lote "
                 f"(un comprobante cubre varias reservas) — {n_rev} a revisar</h3>")
        # Mismatching lotes first (they're the ones to act on).
        for hsh, v in sorted(shared.items(), key=lambda kv: kv[1][0].get("match_flag") == "OK-BULK"):
            ok = v[0].get("match_flag") == "OK-BULK"
            h.append(f"<details class=sub {'open' if not ok else ''}><summary>"
                     f"<span>Recibo {_esc(v[0]['receipt'] or '—')} · esperado(suma) {_esc(lote_suma(v))} · {len(v)} reservas</span>"
                     f"<span class='badge {'ok' if ok else 'mid'}'>{'OK cubre' if ok else 'revisar'}</span></summary><table>"
                     "<tr><th>Reserva</th><th>Huésped</th><th>Check-out</th></tr>")
            for r in sorted(v, key=lambda x: x["checkout"]):
                h.append(f"<tr><td class=cid><a href='{r['url']}' target=_blank>{r['cid']}</a></td>"
                         f"<td>{_esc(r['guest'][:30])}</td><td>{_esc(r['checkout'])}</td></tr>")
            h.append("</table></details>")
    h.append("</div>")
    h.append("<script>function openHash(){var h=location.hash.slice(1);if(!h)return;"
             "var d=document.getElementById(h);if(d&&d.tagName==='DETAILS'){d.open=true;"
             "d.scrollIntoView({behavior:'smooth',block:'start'});}}"
             "addEventListener('hashchange',openHash);addEventListener('load',openHash);</script>")
    return "\n".join(h)


if __name__ == "__main__":
    raise SystemExit(main())
