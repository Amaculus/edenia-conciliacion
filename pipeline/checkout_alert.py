"""Daily WhatsApp alert with every PXSol reservation that checks out on a date.

Data source: PXSol public API (booking/list filtered by checkout date, then
voucher/list per booking). Sender: the whatsapp-bridge REST endpoint.

Usage:
    python checkout_alert.py                 # yesterday (America/Argentina/Buenos_Aires), send
    python checkout_alert.py --dry-run       # print the message, do not send
    python checkout_alert.py --date 2026-09-11 --dry-run
    python checkout_alert.py --recipient 5491149911692@s.whatsapp.net

Environment:
    PXSOL_API_KEY   required
    WA_BRIDGE_URL   default http://127.0.0.1:8765
    WA_RECIPIENT    default: group "Los maculus"
    HOTEL_LABEL     default "Edenia"
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

try:
    import cloudscraper  # PXSol sits behind Cloudflare; plain requests can get a challenge page

    _session = cloudscraper.create_scraper()
except ImportError:  # pragma: no cover
    import requests

    _session = requests.Session()

PXSOL_BASE = "https://gateway-prod.pxsol.com/v2"
PMS_LINK = "https://pms.pxsol.com/bookings/cid.html?CID={booking_id}"
TZ = ZoneInfo("America/Argentina/Buenos_Aires")
DEFAULT_RECIPIENT = "5491131221302-1404327635@g.us"  # WhatsApp group "Los maculus"

STATUS_LABEL = {0: "Desconocido", 1: "Cancelada", 2: "Pendiente/Check-in/Check-out", 3: "Confirmada", 4: "No show"}
PAID_STATUS = {"cobrado", "pagado"}
UNPAID_STATUS = {"a cobrar", "pendiente", "no cobrado", "por cobrar"}


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- PXSol
def pxsol_get(api_key: str, endpoint: str, params: dict, retries: int = 3):
    url = f"{PXSOL_BASE}{endpoint}"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
    last = None
    for attempt in range(1, retries + 1):
        try:
            resp = _session.get(url, params=params, headers=headers, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1), 4))
    raise RuntimeError(f"PXSol {endpoint} failed: {last}")


def fetch_checkouts(api_key: str, day: str) -> list[dict]:
    """All bookings whose checkout equals `day`, across pages."""
    bookings: list[dict] = []
    page = 1
    while True:
        data = pxsol_get(api_key, "/booking/list", {"checkout": day, "per_page": 100, "current_page": page})
        payload = data[0] if isinstance(data, list) and data else data
        rows = payload.get("data") or []
        bookings.extend(rows)
        meta = payload.get("meta") or {}
        last_page = int(meta.get("last_page") or 1)
        if page >= last_page or not rows:
            break
        page += 1
    return bookings


def fetch_vouchers(api_key: str, booking_id: str) -> list[dict]:
    data = pxsol_get(api_key, "/voucher/list", {"booking_id": booking_id})
    rows = data.get("data") if isinstance(data, dict) else data
    out = []
    for r in rows or []:
        attrs = dict(r.get("attributes") or r)
        attrs["id"] = r.get("id") or attrs.get("id")
        out.append(attrs)
    return out


# ---------------------------------------------------------------- formatting
def money(amount: float, currency: str) -> str:
    cur = (currency or "").upper() or "?"
    if cur == "ARS" or abs(amount - round(amount)) < 0.005:
        text = f"{int(round(amount)):,}".replace(",", ".")
    else:
        text = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{cur} {text}"


def fmt_date(value: str | None) -> str:
    if not value:
        return "s/f"
    try:
        return dt.date.fromisoformat(str(value)[:10]).strftime("%d/%m")
    except ValueError:
        return str(value)


METHOD_SHORT = {
    "cuenta corriente": "Cta. corriente",
    "transferencia bancaria": "Transferencia",
    "tarjeta de credito": "Tarjeta credito",
    "tarjeta de debito": "Tarjeta debito",
    "mercadopago": "Mercado Pago",
    "contado": "Efectivo",
}


def clean_methods(raw: str | None) -> str:
    text = str(raw or "").strip("[]").replace("],[", ",").replace("_", " ")
    parts = [p.strip() for p in text.split(",") if p.strip()]
    parts = [METHOD_SHORT.get(p.lower(), p) for p in parts]
    return " + ".join(parts) or "sin metodo"


def summarize_vouchers(vouchers: list[dict]) -> tuple[list[str], dict[str, float], dict[str, float]]:
    """Return (lines, paid_by_currency, unpaid_by_currency). Two short lines per voucher."""
    lines: list[str] = []
    paid: dict[str, float] = {}
    unpaid: dict[str, float] = {}
    for v in vouchers:
        if str(v.get("deleted_at") or "").strip():
            continue
        vtype = str(v.get("voucher_type") or "Comprobante")
        cur = str(v.get("currency") or "?").upper()
        try:
            total = float(v.get("total") or 0)
        except (TypeError, ValueError):
            total = 0.0
        pstatus = str(v.get("payment_status") or "").strip()
        key = pstatus.lower()
        is_credit = "credito" in vtype.lower() or "crédito" in vtype.lower() or bool(v.get("credit_note"))
        who = str(v.get("social_reason") or "").strip()
        method = clean_methods(v.get("payment_type"))
        when = fmt_date(v.get("imputation_date") or v.get("date"))
        if is_credit:
            tag = "nota de credito"
        elif key in PAID_STATUS:
            tag = "cobrado"
            paid[cur] = paid.get(cur, 0.0) + total
        elif key in UNPAID_STATUS:
            tag = "a cobrar"
            unpaid[cur] = unpaid.get(cur, 0.0) + total
        else:
            tag = pstatus.lower() or "sin estado"
        lines.append(f"- {money(total, cur)}, {tag}")
        detail = f"  {vtype}, {method}, {when}"
        lines.append(detail)
        if who:
            lines.append(f"  {who}")
    return lines, paid, unpaid


def verdict(vouchers: list[dict] | None, paid: dict[str, float], unpaid: dict[str, float]) -> str:
    if vouchers is None:
        return "ERROR AL CONSULTAR"
    if not vouchers:
        return "SIN COMPROBANTES"
    has_paid = any(a > 0 for a in paid.values())
    has_unpaid = any(a > 0 for a in unpaid.values())
    if has_paid and has_unpaid:
        return "PARCIAL"
    if has_unpaid:
        return "A COBRAR"
    if has_paid:
        return "COBRADO"
    return "SIN COBROS"


def channel_text(b: dict) -> str:
    origin = str(b.get("origin") or "").strip()
    source = str(b.get("source") or "").strip()
    if source and source.lower() != "desconocido":
        return f"{source} ({origin})" if origin and origin.lower() != source.lower() else source
    return origin or "sin canal"


def room_budgets(b: dict) -> dict[str, float]:
    """Sum of room sub_totals per currency (booking/list has no booking-level currency)."""
    by_cur: dict[str, float] = {}
    for r in b.get("rooms") or []:
        try:
            amt = float(r.get("sub_total") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        rc = str(r.get("sub_total_currency") or "").upper()
        if not rc:
            continue
        by_cur[rc] = by_cur.get(rc, 0.0) + amt
    return by_cur


def booking_expected(b: dict) -> str:
    by_cur = room_budgets(b)
    cur = str(b.get("currency") or "").upper()
    if not cur and len(by_cur) == 1:
        cur = next(iter(by_cur))
    try:
        total = float(b.get("subtotal") or 0)
    except (TypeError, ValueError):
        total = 0.0
    if total > 0 and cur:
        return money(total, cur)
    if by_cur:
        return " + ".join(money(a, c) for c, a in by_cur.items())
    if total > 0:
        return money(total, "?")
    return "sin valor cargado"


def stay_nights(b: dict, day: dt.date) -> str:
    """Nights of the stay. The list's `nights` field adds up every room, so derive it from the dates."""
    try:
        checkin = dt.date.fromisoformat(str(b.get("check_in"))[:10])
        return str(max((day - checkin).days, 0))
    except (TypeError, ValueError):
        return str(b.get("nights") or "?")


def build_message(day: dt.date, bookings: list[dict], api_key: str, hotel_label: str,
                  sess=None, bearer=None) -> str:
    import archivos_check as ac  # lazy: avoids the circular import at module load
    active = [b for b in bookings if int(b.get("status") or 0) != 1]
    cancelled = len(bookings) - len(active)
    day_txt = day.strftime("%d/%m")
    n = len(active)
    head = [f"*{hotel_label.upper()} - CHECK-OUTS {day_txt}*", f"{n} reserva{'s' if n != 1 else ''}", ""]
    if not active:
        body = ["Sin check-outs hoy.", ""]
    else:
        body = []
        for i, b in enumerate(sorted(active, key=lambda x: str(x.get("booking_id"))), start=1):
            bid = str(b.get("booking_id"))
            g = b.get("guest_details") or {}
            guest = " ".join(str(g.get(k) or "").strip() for k in ("name", "last_name")).strip() or "Sin nombre"
            rooms = [str(p.get("name") or "").strip() for p in (b.get("physical_rooms") or [])]
            rooms = [r for r in rooms if r]
            if not rooms:
                room_txt = "Sin habitacion asignada"
            elif len(rooms) > 3:
                room_txt = f"{len(rooms)} habitaciones ({rooms[0]}, ...)"
            else:
                room_txt = "Hab " + ", ".join(rooms)
            nights = stay_nights(b, day)
            checkin = fmt_date(b.get("check_in"))
            try:
                vouchers = fetch_vouchers(api_key, bid)
            except Exception as exc:  # noqa: BLE001
                log(f"voucher/list {bid} failed: {exc}")
                vouchers = None
            lines, paid, unpaid = summarize_vouchers(vouchers or [])
            body.append(f"*{i}. {room_txt}*")
            body.append(guest.title() if guest.isupper() else guest)
            body.append(f"{nights} noche{'' if nights == '1' else 's'}, {checkin} al {day_txt}")
            body.append(channel_text(b))
            # Esperado: total autoritativo del summary si hay bearer; si no, el de rooms.
            esperado = booking_expected(b)
            if bearer:
                try:
                    s = ac.fetch_summary(bearer, bid)
                    if s.get("total") is not None:
                        esperado = f"{s.get('currency') or ''} {s['total']:,.2f}".strip()
                except Exception:  # noqa: BLE001
                    pass
            body.append(f"Esperado: {esperado}")
            body.append(f"Estado: *{verdict(vouchers, paid, unpaid)}*")
            body.extend(lines)
            # Pago real (libro de pagos) y comprobante subido (pestaña Archivos).
            if sess is not None:
                try:
                    ps = ac.pagos_summary(ac.fetch_pagos(sess, bid))
                    if ps.get("pagos_n"):
                        body.append(f"Pago: {ps['pagos_cobrado']} ({ps['pagos_metodos']})")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    state, nfiles, _ = ac.fetch_archivos_http(sess, bid)
                    body.append("Comprobante: " + (f"Si ({nfiles})" if state == "con_archivos" else "No"))
                except Exception:  # noqa: BLE001
                    pass
            body.append(PMS_LINK.format(booking_id=bid))
            body.append("")
    foot = []
    if cancelled:
        foot.append(f"Canceladas con check-out {day_txt}: {cancelled}")
    foot.append(f"PXSol, {dt.datetime.now(TZ).strftime('%d/%m %H:%M')}")
    return "\n".join(head + body + foot).strip()


# ---------------------------------------------------------------- WhatsApp
def send_whatsapp(bridge_url: str, recipient: str, message: str) -> None:
    body = json.dumps({"recipient": recipient, "message": message}).encode()
    req = urllib.request.Request(
        f"{bridge_url.rstrip('/')}/api/send", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode())
    if not result.get("success"):
        raise RuntimeError(f"bridge refused the message: {result}")
    log(f"sent to {recipient}: {result.get('message')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="checkout date YYYY-MM-DD (default: yesterday in Buenos Aires, 'a dia vencido')")
    ap.add_argument("--dry-run", action="store_true", help="print the message, do not send")
    ap.add_argument("--recipient", default=os.environ.get("WA_RECIPIENT", DEFAULT_RECIPIENT))
    ap.add_argument("--bridge-url", default=os.environ.get("WA_BRIDGE_URL", "http://127.0.0.1:8765"))
    args = ap.parse_args()

    try:
        import archivos_check as ac
        ac._load_dotenv()  # load ../.env (PXSOL_API_KEY, web login, etc.)
    except Exception:  # noqa: BLE001
        pass
    api_key = os.environ.get("PXSOL_API_KEY", "").strip()
    if not api_key:
        log("PXSOL_API_KEY is not set")
        return 2
    hotel_label = os.environ.get("HOTEL_LABEL", "Edenia")
    # "a dia vencido": the morning message reports the check-outs of the previous day
    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(TZ).date() - dt.timedelta(days=1)

    log(f"fetch checkouts for {day}")
    bookings = fetch_checkouts(api_key, day.isoformat())
    log(f"{len(bookings)} bookings (all statuses)")
    # Enrich with the real payment ledger + uploaded-receipt status (best effort).
    sess = bearer = None
    active = [b for b in bookings if int(b.get("status") or 0) != 1]
    if active:
        try:
            import archivos_check as ac
            first = str(active[0].get("booking_id"))
            sess = ac.ensure_http_session(ac.OUT_DIR / "state.json", first)
            bearer = ac.capture_bearer(ac.OUT_DIR / "state.json", first)
        except Exception as exc:  # noqa: BLE001
            log(f"enrichment session unavailable ({exc}); sending base message")
            sess = bearer = None
    message = build_message(day, bookings, api_key, hotel_label, sess=sess, bearer=bearer)
    print("-" * 60)
    print(message)
    print("-" * 60)
    if args.dry_run:
        log("dry-run: not sent")
        return 0
    send_whatsapp(args.bridge_url, args.recipient, message)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f"FAILED: {exc}")
        sys.exit(1)
