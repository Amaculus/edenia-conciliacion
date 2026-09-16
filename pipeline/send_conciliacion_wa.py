"""Format the reconciliation summary (report.py --json) into a WhatsApp message and
send it through the local wa-bridge. Runs after conciliar.sh in the nightly cron.

Usage:
    python send_conciliacion_wa.py --json out/hallazgos.json            # send
    python send_conciliacion_wa.py --json out/hallazgos.json --dry-run  # just print
    python send_conciliacion_wa.py --json out/hallazgos.json --recipient 5491131221302

Environment:
    WA_BRIDGE_URL   default http://localhost:8765/api/send
    WA_RECIPIENT    default: group "Los maculus"
"""
from __future__ import annotations

import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEFAULT_RECIPIENT = "5491131221302-1404327635@g.us"  # grupo Los maculus
BRIDGE = os.environ.get("WA_BRIDGE_URL", "http://localhost:8765/api/send")

DOT = {"cobro_no_registrado": "🔴", "chase_directo_agencia": "🔴", "monto_no_coincide": "🟠",
       "comprobante_ilegible": "🟠", "chase_ota": "⚪", "cobrado_sin_respaldo": "⚪", "conciliado": "✅"}
NAME = {"cobro_no_registrado": "Cobro no registrado", "chase_directo_agencia": "Sin cobrar Dir/Agencia",
        "monto_no_coincide": "Monto no coincide", "comprobante_ilegible": "Comprobante ilegible",
        "chase_ota": "Sin cobrar OTA", "cobrado_sin_respaldo": "Cobrado sin comprobante",
        "conciliado": "Conciliado"}


def money(cur_map: dict) -> str:
    return ", ".join(f"{c} {a:,.0f}" for c, a in cur_map.items()) or "—"


def build(s: dict) -> str:
    c = s["counts"]
    L = [f"*Edenia — Conciliación {s['generado'][:10]}*  ({s['total_checkouts']} check-outs)", ""]
    L.append("*Para accionar:*")
    for k in ("cobro_no_registrado", "chase_directo_agencia", "monto_no_coincide", "comprobante_ilegible"):
        if c.get(k):
            L.append(f"{DOT[k]} {NAME[k]}: {c[k]}")
    lot = s.get("lotes", {})
    if lot.get("a_revisar"):
        L.append(f"🟠 Lotes a revisar: {lot['a_revisar']} de {lot['total']}")
    L.append("")
    L.append(f"Sin cobrar OTA: {c.get('chase_ota',0)} · Cobrado s/comprob: {c.get('cobrado_sin_respaldo',0)} "
             f"· Conciliado: {c.get('conciliado',0)}")
    L.append(f"*Saldo pendiente:* {money(s.get('saldo_pendiente',{}))}")
    # Caja por método (moneda principal = la de mayor movimiento)
    caja = s.get("caja_por_metodo", {})
    if caja:
        cur = max(caja, key=lambda x: sum(caja[x].values()))
        top3 = list(caja[cur].items())[:3]
        L.append("*Caja " + cur + ":* " + ", ".join(f"{m} {v:,.0f}" for m, v in top3))
    # Top prioridad (juntar los tres buckets de acción)
    pr = s.get("prioridad", {})
    items = (pr.get("cobro_no_registrado", []) + pr.get("chase_directo_agencia", [])
             + pr.get("monto_no_coincide", []))
    if items:
        L.append("")
        L.append("*Top:*")
        for r in items[:8]:
            d = f" +{r['dias']}d" if r.get("dias", -1) >= 0 else ""
            extra = f" — {r['detalle']}" if r.get("detalle") else ""
            L.append(f"• {r['cid']} {r['guest'][:22]} [{r['channel']}] esperado {r['esperado'] or '-'}{d}{extra[:50]}")
    if s.get("link"):
        L.append("")
        L.append(f"Reporte completo: {s['link']}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", dest="json_in", required=True)
    ap.add_argument("--recipient", default=os.environ.get("WA_RECIPIENT", DEFAULT_RECIPIENT))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    s = json.loads(open(args.json_in, encoding="utf-8").read())
    msg = build(s)
    if args.dry_run:
        print(msg)
        return 0
    import requests
    r = requests.post(BRIDGE, json={"recipient": args.recipient, "message": msg}, timeout=30)
    print(f"WhatsApp -> {args.recipient}: {r.status_code} {r.text[:120]}")
    return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
