"""Check the "Archivos" tab of every PXSol reservation that checked out in a date range.

Engine: the Archivos tab is rendered by one backend call,
`pms.pxsol.com/bookings/files.php?CID=<id>`. Called with the login session
cookie it returns the file list, so `check` reads it over plain HTTP - no browser
per reservation. The public API (gateway-prod.pxsol.com/v2) has no files endpoint.
Verified 2026-09-16.

Session: PXSol login is Auth0. The browser (Playwright) is used only to mint/refresh
the session cookie into out/state.json; that cookie lasts ~1 week. `check` re-mints
automatically if the cookie is missing or expired.

Usage:
    python archivos_check.py login                      # mint the session (--manual for a window)
    python archivos_check.py explore 11454577           # dumps the booking page + Archivos tab (browser)
    python archivos_check.py check --days 60            # all check-outs in the last 60 days (http)
    python archivos_check.py check --date 2026-09-14    # one day
    python archivos_check.py check --days 60 --out report.csv --concurrency 8

Environment:
    PXSOL_API_KEY     required for `check` (booking list comes from the API)
    PXSOL_WEB_USER / PXSOL_WEB_PASS  PMS login, for auto-minting the cookie
    PXSOL_PROFILE_DIR default ~/.pxsol-playwright-profile
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
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checkout_alert import (  # noqa: E402
    channel_text,
    fetch_checkouts,
    fetch_vouchers,
    summarize_vouchers,
    verdict,
)

PMS_HOME = "https://pms.pxsol.com/"
PMS_BOOKING = "https://pms.pxsol.com/bookings/cid.html?CID={cid}"
TZ = ZoneInfo("America/Argentina/Buenos_Aires")
PROFILE_DIR = Path(os.environ.get("PXSOL_PROFILE_DIR") or Path.home() / ".pxsol-playwright-profile")
OUT_DIR = Path(__file__).resolve().parent / "out"


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from the repo .env into os.environ (no override)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_dotenv()

CANCELLED_STATUS = {1}


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def is_logged_in(page: Page) -> bool:
    return "pms.pxsol.com" in page.url and "auth0" not in page.url


def _click_continue(page: Page) -> None:
    for finder in (
        lambda: page.get_by_role("button", name=re.compile(r"continuar|continue", re.I)),
        lambda: page.locator("button[type=submit][name=action]"),
        lambda: page.locator("button[type=submit]"),
    ):
        loc = finder()
        if loc.count() > 0:
            loc.first.click()
            return
    raise RuntimeError("Continue button not found on Auth0 page")


def auto_login(page: Page, timeout_s: int = 60) -> bool:
    """Log in through the PXSol Auth0 flow using PXSOL_WEB_USER / PXSOL_WEB_PASS."""
    user = os.environ.get("PXSOL_WEB_USER")
    pw = os.environ.get("PXSOL_WEB_PASS")
    if not (user and pw):
        raise RuntimeError("PXSOL_WEB_USER / PXSOL_WEB_PASS not set")
    page.goto(PMS_HOME, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    if is_logged_in(page):
        return True
    # Identifier step: fill username if the field is present and empty.
    uname = page.locator("input#username, input[name=username]")
    if uname.count() > 0 and not (uname.first.input_value() or "").strip():
        uname.first.fill(user)
        _click_continue(page)
        page.wait_for_timeout(2500)
    # Password step.
    pwd = page.locator("input#password, input[name=password], input[type=password]")
    try:
        pwd.first.wait_for(state="visible", timeout=15000)
    except PWTimeout:
        pass
    if pwd.count() > 0:
        pwd.first.fill(pw)
        _click_continue(page)
    # Wait for the redirect back to the PMS.
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if is_logged_in(page):
            page.wait_for_timeout(2500)
            return True
        page.wait_for_timeout(1000)
    return is_logged_in(page)


def ensure_session(page: Page) -> None:
    if is_logged_in(page):
        return
    log("No live session, logging in via Auth0...")
    if not auto_login(page):
        raise RuntimeError(f"Auto-login failed. Current URL: {page.url}")
    log("Logged in.")


# ---------------------------------------------------------------- browser
def open_context(pw, headed: bool):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return pw.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=not headed,
        viewport={"width": 1400, "height": 1000},
        locale="es-AR",
        args=["--disable-blink-features=AutomationControlled"],
    )


def cmd_login(args) -> int:
    """Log in and persist the session. Automated by default; --manual opens a window."""
    with sync_playwright() as pw:
        ctx = open_context(pw, headed=args.headed or args.manual)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if args.manual:
            page.goto(PMS_HOME, wait_until="domcontentloaded")
            log("Log in in the browser window. This script waits up to 10 minutes.")
            deadline = time.time() + 600
            while time.time() < deadline:
                if is_logged_in(page):
                    page.wait_for_timeout(3000)
                    log(f"Logged in. Profile saved at {PROFILE_DIR}")
                    ctx.close()
                    return 0
                page.wait_for_timeout(1000)
            log("Timeout. Not logged in.")
            ctx.close()
            return 1
        ok = auto_login(page)
        if ok:
            log(f"Logged in. URL: {page.url}. Profile saved at {PROFILE_DIR}")
        else:
            log(f"Auto-login failed. URL: {page.url}")
            page.screenshot(path=str(OUT_DIR / "login_fail.png"), full_page=True)
        ctx.close()
        return 0 if ok else 1


def goto_booking(page: Page, cid: str) -> None:
    def _load_and_settle() -> None:
        page.goto(PMS_BOOKING.format(cid=cid), wait_until="domcontentloaded", timeout=60000)
        # The SPA polls constantly and never reaches networkidle; wait for the
        # reservation header text instead, then a short settle.
        try:
            page.get_by_text("Datos de la reserva").first.wait_for(state="visible", timeout=20000)
        except PWTimeout:
            page.wait_for_timeout(3000)
        page.wait_for_timeout(700)

    _load_and_settle()
    if not is_logged_in(page):
        ensure_session(page)
        _load_and_settle()


EMPTY_MARKER = "No hay archivos subidos"
# Each uploaded file is a div.fileitem carrying the filename in list-file="...".
FILE_ITEM_SEL = "[list-file]"
PANEL_READY = re.compile(r"No hay archivos subidos|Biblioteca de Archivos|Subir archivo", re.I)


def click_archivos(page: Page) -> bool:
    """Click the visible "Archivos" tab and wait for the panel. Return True when ready."""
    # Wait for the tab bar to exist (the reservation body is loaded).
    try:
        page.get_by_text("Historial", exact=True).first.wait_for(state="visible", timeout=15000)
    except PWTimeout:
        pass
    loc = page.get_by_text("Archivos", exact=True)
    target = None
    for i in range(loc.count()):
        el = loc.nth(i)
        try:
            if el.is_visible():
                target = el
                break
        except Exception:  # noqa: BLE001
            continue
    if target is None:
        return False
    try:
        target.scroll_into_view_if_needed(timeout=4000)
    except Exception:  # noqa: BLE001
        pass
    try:
        target.click(timeout=8000)
    except Exception:  # noqa: BLE001
        target.click(timeout=8000, force=True)
    # Wait for the Archivos panel to render (empty marker, library header, or a file item).
    ready = page.locator(FILE_ITEM_SEL).or_(page.get_by_text(PANEL_READY))
    try:
        ready.first.wait_for(state="visible", timeout=12000)
    except PWTimeout:
        page.wait_for_timeout(1500)
    return True


def archivos_state(page: Page) -> tuple[str, int, list[str]]:
    """Return (state, count, names). state in {vacio, con_archivos, incierto}.

    Uploaded files are div.fileitem[list-file="<name>"]. Empty tab shows
    "No hay archivos subidos". Anything else is "incierto".
    """
    names: list[str] = []
    for el in page.query_selector_all(FILE_ITEM_SEL):
        name = (el.get_attribute("list-file") or "").strip()
        if name:
            names.append(name)
    names = list(dict.fromkeys(names))
    if names:
        return "con_archivos", len(names), names
    try:
        body = page.inner_text("body")
    except Exception:  # noqa: BLE001
        body = ""
    if EMPTY_MARKER.lower() in body.lower():
        return "vacio", 0, []
    return "incierto", 0, []


def cmd_explore(args) -> int:
    OUT_DIR.mkdir(exist_ok=True)
    cid = args.cid
    with sync_playwright() as pw:
        ctx = open_context(pw, headed=args.headed)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        goto_booking(page, cid)
        page.screenshot(path=str(OUT_DIR / f"explore_{cid}_1.png"), full_page=True)
        tabs = [t.inner_text().strip() for t in page.query_selector_all("[role=tab], .nav-tabs a, .tab, ul.tabs li")]
        log(f"Tab-like elements: {tabs}")
        found = click_archivos(page)
        log(f"Archivos tab clicked: {found}")
        page.screenshot(path=str(OUT_DIR / f"explore_{cid}_2.png"), full_page=True)
        (OUT_DIR / f"explore_{cid}.html").write_text(page.content(), encoding="utf-8")
        (OUT_DIR / f"explore_{cid}.txt").write_text(page.inner_text("body"), encoding="utf-8")
        state, n, names = archivos_state(page)
        log(f"Archivos state: {state} ({n} files) {names}")
        log(f"Dumps in {OUT_DIR}")
        ctx.close()
    return 0


# ---------------------------------------------------------------- HTTP engine
# The Archivos tab is rendered by one backend call. Called with the login
# session cookie it returns the file list as an HTML fragment, so we can skip
# the browser entirely and just parse it. Verified 2026-09-16.
FILES_URL = "https://pms.pxsol.com/bookings/files.php?CID={cid}&time={t}"


class SessionExpired(Exception):
    pass


def http_session(state_path: Path) -> requests.Session:
    st = json.loads(state_path.read_text(encoding="utf-8"))
    s = requests.Session()
    for c in st.get("cookies", []):
        if "pxsol.com" in c["domain"]:
            s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
    s.headers.update({
        "User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://pms.pxsol.com/",
    })
    return s


def parse_files_html(html: str) -> tuple[str, int, list[str]]:
    names = list(dict.fromkeys(re.findall(r'list-file="([^"]+)"', html)))
    is_files_page = bool(names) or ("Subir archivo" in html) or (EMPTY_MARKER in html)
    if not is_files_page:  # got the login shell instead of the fragment
        raise SessionExpired()
    if names:
        return "con_archivos", len(names), names
    if EMPTY_MARKER in html:
        return "vacio", 0, []
    return "incierto", 0, []


PAGOS_URL = "https://pms.pxsol.com/bookings/pagos_pms.php?CID={cid}&time={t}"


def _amt(s: str):
    m = re.search(r"([A-Z]{3})\s*\$?\s*([\d.,]+)", s or "")
    if not m:
        return None, None
    try:
        return m.group(1), float(m.group(2).replace(",", ""))
    except ValueError:
        return m.group(1), None


def fetch_pagos(session: requests.Session, cid: str) -> list[dict]:
    """Parse the payment ledger (pagos_pms.php): one dict per registered payment."""
    try:
        html = session.get(PAGOS_URL.format(cid=cid, t=int(time.time() * 1000)), timeout=30).text
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        cells = [c for c in cells if c]
        if len(cells) < 6 or cells[0] == "Fecha":
            continue
        cur, inc = _amt(cells[4]); _, egr = _amt(cells[5])
        fx = re.search(r"=\s*USD\s*([\d.,]+)", cells[4])
        rows.append({"cid": cid, "fecha": cells[0], "metodo": cells[1], "concepto": cells[2][:60],
                     "moneda": cur, "ingreso": inc, "egreso": egr,
                     "usd": float(fx.group(1)) if fx else None, "usuario": cells[6] if len(cells) > 6 else ""})
    return rows


def pagos_summary(rows: list[dict]) -> dict:
    """Collapse the ledger into per-reservation fields."""
    if not rows:
        return {"pagos_n": 0, "pagos_metodos": "", "pagos_cobrado": "", "pagos_ultimo": ""}
    net = {}
    for r in rows:
        cur = r.get("moneda") or "?"
        net[cur] = net.get(cur, 0) + (r.get("ingreso") or 0) - (r.get("egreso") or 0)
    metodos = " + ".join(dict.fromkeys(re.sub(r"\s*\(.*?\)", "", r["metodo"]).strip() for r in rows if r.get("metodo")))
    cobrado = ", ".join(f"{c} {a:,.2f}" for c, a in net.items() if abs(a) > 0.005)
    ultimo = max((r["fecha"] for r in rows), default="")
    return {"pagos_n": len(rows), "pagos_metodos": metodos, "pagos_cobrado": cobrado, "pagos_ultimo": ultimo}


def fetch_archivos_http(session: requests.Session, cid: str, attempts: int = 3) -> tuple[str, int, list[str]]:
    last = ("http-error: no attempt", -1, [])
    for _ in range(attempts):
        try:
            r = session.get(FILES_URL.format(cid=cid, t=int(time.time() * 1000)), timeout=30)
            if r.status_code != 200:
                last = (f"http-error: {r.status_code}", -1, [])
            else:
                return parse_files_html(r.text)
        except SessionExpired:
            raise
        except Exception as exc:  # noqa: BLE001
            last = (f"http-error: {exc}", -1, [])
        time.sleep(0.5)
    return last


def mint_session(state_path: Path) -> None:
    """Log in headless via Playwright and export cookies to state_path."""
    with sync_playwright() as pw:
        ctx = open_context(pw, headed=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ensure_session(page)
        ctx.storage_state(path=str(state_path))
        ctx.close()


def ensure_http_session(state_path: Path, probe_cid: str | None) -> requests.Session:
    """Return a valid HTTP session, re-minting cookies if missing or expired."""
    if not state_path.exists():
        log("No saved session; logging in to mint cookies...")
        mint_session(state_path)
    sess = http_session(state_path)
    if probe_cid:
        try:
            fetch_archivos_http(sess, probe_cid, attempts=1)
        except SessionExpired:
            log("Session expired; re-logging in...")
            mint_session(state_path)
            sess = http_session(state_path)
    return sess


# -------------- authoritative reservation totals (internal REST API, needs a bearer)
SUMMARY_URL = "https://api-2-pms-prod.pxsol.com/api/v2/reservations/{cid}/summary"
BEARER_FILE = OUT_DIR / "bearer.txt"


def capture_bearer(state_path: Path, cid: str, max_age_s: int = 21600) -> str | None:
    """Grab the Auth0 access token the SPA sends to the internal API. Cached ~6h."""
    if BEARER_FILE.exists() and (time.time() - BEARER_FILE.stat().st_mtime) < max_age_s:
        tok = BEARER_FILE.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    tok = {"v": None}
    with sync_playwright() as pw:
        ctx = open_context(pw, headed=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_req(r):
            if not tok["v"] and ("api-2-pms-prod.pxsol.com" in r.url or "api-1-pms-prod.pxsol.io" in r.url):
                a = r.headers.get("authorization")
                if a and a.lower().startswith("bearer "):
                    tok["v"] = a.split(" ", 1)[1]

        page.on("request", on_req)
        try:
            page.goto(PMS_BOOKING.format(cid=cid), wait_until="domcontentloaded", timeout=60000)
            for _ in range(20):
                if tok["v"]:
                    break
                page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass
        ctx.close()
    if tok["v"]:
        BEARER_FILE.write_text(tok["v"], encoding="utf-8")
    return tok["v"]


def _find_totals(obj):
    """Walk the summary JSON for the object that carries 'unpaid' (the money block)."""
    if isinstance(obj, dict):
        if "unpaid" in obj and ("total" in obj or "total_net" in obj):
            return obj
        for v in obj.values():
            r = _find_totals(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_totals(v)
            if r:
                return r
    return None


def fetch_summary(bearer: str, cid: str) -> dict:
    """Return {total, paid, unpaid, currency} from the reservation summary, or {}."""
    if not bearer:
        return {}
    try:
        r = requests.get(SUMMARY_URL.format(cid=cid),
                         headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"}, timeout=30)
        if r.status_code != 200:
            return {}
        blk = _find_totals(r.json()) or {}
        def num(x):
            try:
                return round(float(x), 2)
            except (TypeError, ValueError):
                return None
        return {"total": num(blk.get("total") or blk.get("total_net")), "paid": num(blk.get("paid")),
                "unpaid": num(blk.get("unpaid")), "currency": blk.get("currency")}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------- check
def date_range(args) -> list[str]:
    if args.date:
        return [args.date]
    today = dt.datetime.now(TZ).date()
    return [(today - dt.timedelta(days=i)).isoformat() for i in range(1, args.days + 1)]


def build_worklist(api_key: str, days: list[str], skip: set[str] | None = None) -> list[dict]:
    """API-only pass (fast, serial): one task dict per non-cancelled reservation.

    `skip` = cids already done (resume): they are excluded before the per-booking
    voucher call, so a resume does not re-query the API for finished reservations.
    """
    skip = skip or set()
    work: list[dict] = []
    for day in days:
        bookings = fetch_checkouts(api_key, day)
        n_task = 0
        for b in bookings:
            cid = str(b.get("id") or b.get("booking_id") or "")
            status = int(b.get("status") or 0)
            if not cid or status in CANCELLED_STATUS or cid in skip:
                continue
            g = b.get("guest_details") or {}
            guest = " ".join(str(g.get(k) or "").strip() for k in ("name", "last_name")).strip() or "Sin nombre"
            guest = guest.title() if guest.isupper() else guest
            try:
                vouchers = fetch_vouchers(api_key, cid)
                _, paid, unpaid = summarize_vouchers(vouchers)
                api_verdict = verdict(vouchers, paid, unpaid)
            except Exception as exc:  # noqa: BLE001
                api_verdict = f"API ERROR {exc}"
            work.append({"checkout": day, "cid": cid, "guest": guest,
                         "channel": channel_text(b), "api_verdict": api_verdict})
            n_task += 1
        log(f"{day}: {len(bookings)} check-outs, {n_task} to inspect")
    return work


FIELDNAMES = ["checkout", "cid", "guest", "channel", "api_verdict",
              "total", "paid", "unpaid", "currency",
              "pagos_n", "pagos_metodos", "pagos_cobrado", "pagos_ultimo",
              "archivos", "n_archivos", "archivo_names", "flag", "url"]
GOOD_STATES = {"vacio", "con_archivos"}  # a reservation is "done" only if its tab read cleanly


class Checkpoint:
    """Durable, thread-safe, append-as-you-go CSV. Enables resume after a crash/OOM."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.rows: dict[str, dict] = {}  # cid -> latest row (in memory)
        if path.exists():
            with path.open(newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    self.rows[r["cid"]] = r  # last write wins
        self._fh = path.open("a", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._fh, fieldnames=FIELDNAMES, extrasaction="ignore")
        if path.stat().st_size == 0:
            self._w.writeheader(); self._fh.flush()

    def done_cids(self) -> set[str]:
        return {cid for cid, r in self.rows.items() if r.get("archivos") in GOOD_STATES}

    def append(self, row: dict) -> None:
        with self.lock:
            self._w.writerow(row); self._fh.flush()
            self.rows[row["cid"]] = row

    def finalize(self) -> list[dict]:
        """Rewrite the CSV deduped by cid (best row per reservation), newest first."""
        self._fh.close()
        merged = list(self.rows.values())
        merged.sort(key=lambda r: (r.get("checkout", ""), r.get("cid", "")), reverse=True)
        with self.path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDNAMES, extrasaction="ignore")
            w.writeheader(); w.writerows(merged)
        return merged


def make_row(task: dict, state: str, n_files: int, names: list[str]) -> dict:
    api_verdict = task["api_verdict"]
    if state not in GOOD_STATES:
        flag = "ERROR-LECTURA"  # not read cleanly; resume will retry it
    elif api_verdict in ("A COBRAR", "PARCIAL", "SIN COMPROBANTES") or api_verdict.startswith("API ERROR"):
        flag = "REVISAR-PLATA"
    elif api_verdict == "COBRADO" and state == "vacio":
        flag = "COBRADO-SIN-ARCHIVO"
    else:
        flag = "OK"
    return {
        "checkout": task["checkout"], "cid": task["cid"], "guest": task["guest"],
        "channel": task["channel"], "api_verdict": api_verdict,
        "total": task.get("total"), "paid": task.get("paid"),
        "unpaid": task.get("unpaid"), "currency": task.get("currency"),
        "pagos_n": task.get("pagos_n"), "pagos_metodos": task.get("pagos_metodos"),
        "pagos_cobrado": task.get("pagos_cobrado"), "pagos_ultimo": task.get("pagos_ultimo"),
        "archivos": state, "n_archivos": n_files, "archivo_names": " | ".join(names),
        "flag": flag, "url": PMS_BOOKING.format(cid=task["cid"]),
    }


def cmd_check(args) -> int:
    api_key = os.environ.get("PXSOL_API_KEY")
    if not api_key:
        log("PXSOL_API_KEY missing")
        return 2
    OUT_DIR.mkdir(exist_ok=True)
    days = date_range(args)
    out = Path(args.out) if args.out else OUT_DIR / f"archivos_{days[-1]}_{days[0]}.csv"

    # Resume: skip reservations already read cleanly in a prior run of this CSV.
    ckpt = Checkpoint(out)
    done = ckpt.done_cids()
    log(f"Building work list from API for {len(days)} day(s) (skipping {len(done)} done)...")
    pending = build_worklist(api_key, days, skip=done)
    base = len(done)
    total = base + len(pending)
    log(f"{total} reservations; {base} already done, {len(pending)} to inspect "
        f"(http, concurrency={args.concurrency})")

    if pending:
        # Engine: one backend call per reservation (files.php) with the login cookie.
        # Browser is used only to mint/refresh that cookie.
        state_path = OUT_DIR / "state.json"
        sess = ensure_http_session(state_path, pending[0]["cid"])

        # Authoritative totals: capture a bearer once, then summary per reservation.
        bearer = capture_bearer(state_path, pending[0]["cid"])
        log(f"bearer for totals: {'ok' if bearer else 'unavailable (esperado will be blank)'}")
        if bearer:
            for t in pending:
                t.update(fetch_summary(bearer, t["cid"]))

        counter = {"done": base}
        lock = threading.Lock()
        pagos_all: list[dict] = []

        def worker(task: dict) -> None:
            try:
                state, n_files, names = fetch_archivos_http(sess, task["cid"])
            except SessionExpired:
                state, n_files, names = "sesion-expirada", -1, []
            pagos = fetch_pagos(sess, task["cid"])       # real payment ledger
            task.update(pagos_summary(pagos))
            row = make_row(task, state, n_files, names)
            ckpt.append(row)
            with lock:
                pagos_all.extend(pagos)
                counter["done"] += 1
                log(f"[{counter['done']}/{total}] {task['cid']} {task['guest'][:24]:24} "
                    f"api={task['api_verdict']:16} archivos={state:14} pagos={task.get('pagos_n',0)} -> {row['flag']}")

        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            list(pool.map(worker, pending))

        # Detailed payment ledger (one row per payment) for the cash view.
        if pagos_all:
            pcsv = OUT_DIR / "pagos.csv"
            with pcsv.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=["cid", "fecha", "metodo", "concepto", "moneda",
                                                   "ingreso", "egreso", "usd", "usuario"])
                w.writeheader(); w.writerows(pagos_all)
            log(f"payment ledger: {len(pagos_all)} pagos -> {pcsv}")

    rows = ckpt.finalize()
    flagged = [r for r in rows if r["flag"] != "OK"]
    log(f"{len(rows)} reservations checked, {len(flagged)} to review. CSV: {out}")
    for r in flagged:
        print(f"{r['checkout']} {r['cid']} {r['guest']} [{r['channel']}] "
              f"api={r['api_verdict']} archivos={r['archivos']} -> {r['flag']}  {r['url']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login")
    lg.add_argument("--manual", action="store_true", help="open a window for manual/Google login")
    lg.add_argument("--headed", action="store_true")
    ex = sub.add_parser("explore")
    ex.add_argument("cid")
    ex.add_argument("--headed", action="store_true")
    ck = sub.add_parser("check")
    ck.add_argument("--days", type=int, default=60)
    ck.add_argument("--date")
    ck.add_argument("--out")
    ck.add_argument("--headed", action="store_true")
    ck.add_argument("--delay-ms", type=int, default=800)
    ck.add_argument("--concurrency", type=int, default=8, help="parallel HTTP workers (default 8)")
    args = ap.parse_args()
    return {"login": cmd_login, "explore": cmd_explore, "check": cmd_check}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
