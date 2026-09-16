"""Download and read the files attached to PXSol reservations, and decide whether a
payment receipt (comprobante de pago/transferencia) is present.

Pipeline (all after the fast presence-check in archivos_check.py):
  1. For each reservation with files, GET files.php, extract each file's signed S3
     download URL, download the bytes (cached under out/comprobantes/<cid>/).
  2. Read each file:
       - PDF   -> text via PyMuPDF (digital invoices/receipts carry a text layer)
       - image -> the `claude` CLI vision reader (no OCR engine installed)
       - xlsx/docx/csv -> local text (openpyxl/python-docx); usually rooming lists
  3. Classify each file as a receipt or not, and pull amount + date when it is one.
  4. Per reservation: has_receipt, the receipt amount/date, and the expected amount
     from the API, so a human (or a later step) can judge veracidad.

Usage:
    python read_comprobantes.py --from out/archivos_60d.csv          # only con_archivos rows
    python read_comprobantes.py --days 60                            # (re)build the list first
    python read_comprobantes.py --cid 11496130                       # one reservation
    python read_comprobantes.py --from out/archivos_60d.csv --no-images   # skip CLI vision

Output: out/comprobantes.csv (per reservation) + out/comprobantes_files.csv (per file).
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archivos_check as ac  # noqa: E402
import checkout_alert as ca  # noqa: E402

OUT = ac.OUT_DIR
CACHE = OUT / "comprobantes"
STATE = OUT / "state.json"

# Weak vocabulary (Spanish + English); needs the not-receipt gate to avoid false hits.
RECEIPT_WORDS = re.compile(
    r"transfer|importe|\bop\b|recibo|boleta|\bpago\b|mercado\s*pago|mercadopago|"
    r"receipt|payment|\bpaid\b|invoice|amount", re.I)
# Strong phrases: on their own they identify a comprobante and OVERRIDE the not-receipt gate.
STRONG_WORDS = re.compile(
    r"comprobante|transferenc|orden de pago|constancia|dep[oó]sito|voucher de pago|"
    r"customer receipt|payment receipt", re.I)
NOT_RECEIPT_WORDS = re.compile(
    r"rooming|room\s*list|pasaporte|passport|\bdni\b|check.?in|itinerario|boarding|visa\b", re.I)
# Structural markers mandatory on Argentine transfer receipts; ~0 false positives on
# passports/rooming lists, so they also OVERRIDE the not-receipt gate.
CUIT_PATTERN = re.compile(r"\b\d{2}-\d{8}-\d\b")
CBU_CVU_22 = re.compile(r"\b\d{22}\b")
STRUCT_LABEL = re.compile(r"cu[ií]t|cu[ií]l|\bcbu\b|\bcvu\b|coelsa|n[uú]mero de (?:operaci|comprobante)", re.I)


def has_structural(text: str) -> bool:
    return bool(CUIT_PATTERN.search(text) or CBU_CVU_22.search(text) or STRUCT_LABEL.search(text))


TESSDATA_DIR = Path(__file__).resolve().parent / "tessdata"


def _tesseract_cmd() -> str | None:
    env = os.environ.get("TESSERACT_CMD")
    if env and Path(env).exists():
        return env
    for c in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
              r"C:\Users\Antonio\AppData\Local\Programs\Tesseract-OCR\tesseract.exe",
              "tesseract"):
        try:
            subprocess.run([c, "--version"], capture_output=True, timeout=10)
            return c
        except Exception:  # noqa: BLE001
            continue
    return None


TESSERACT = _tesseract_cmd()
# Amounts in Argentine (1.234,56) or US (1,234.56 / 980.00) format.
_NUM = r"(\d[\d.,\s]*(?:[.,]\d{2}))"
AMOUNT_RE = re.compile(r"(?:importe|monto|total|abonad[oa]|pagad[oa]|amount|paid|price)[^\d]{0,12}\$?\s*" + _NUM, re.I)
AMOUNT_FALLBACK = re.compile(r"\$\s*" + _NUM)
DATE_RE = re.compile(r"(\d{2}[/-]\d{2}[/-]\d{4})|(\d{4}-\d{2}-\d{2})")
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
PDF_EXT = {".pdf"}
SHEET_EXT = {".xlsx", ".xls", ".csv"}
DOC_EXT = {".docx", ".doc"}


def log(m: str) -> None:
    print(m, flush=True)


# ---------------------------------------------------------------- fetch/download
def extract_files(html: str) -> list[tuple[str, str]]:
    """Return [(filename, download_url)] from a files.php fragment."""
    out: list[tuple[str, str]] = []
    seen = set()
    for href in re.findall(r"https://files-private\.s3[^\"'\\ ]+", html):
        h = href.replace("&amp;", "&")
        name = None
        m = re.search(r"filename\*?%3DUTF-8%27%27([^&]+)", h) or re.search(r"filename%3D%22(.+?)%22", h)
        if m:
            name = urllib.parse.unquote(m.group(1))
        if not name:
            name = h.rsplit("/", 1)[-1].split("?")[0]
        key = (name, h[:80])
        if key in seen:
            continue
        seen.add(key)
        out.append((name, h))
    return out


def download(session: requests.Session, cid: str, name: str, url: str) -> Path:
    d = CACHE / cid
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    p = d / safe
    if p.exists() and p.stat().st_size > 0:
        return p
    r = session.get(url, timeout=90)
    r.raise_for_status()
    p.write_bytes(r.content)
    return p


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------- readers
def read_pdf(p: Path) -> str:
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(p)
        text = "\n".join(pg.get_text() for pg in doc)
        doc.close()
        return text
    except Exception as exc:  # noqa: BLE001
        return f"[pdf-error: {exc}]"


def read_sheet(p: Path) -> str:
    try:
        if p.suffix.lower() == ".csv":
            return p.read_text(encoding="utf-8", errors="ignore")[:5000]
        import openpyxl
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        chunks = []
        for ws in wb.worksheets[:2]:
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i > 60:
                    break
                chunks.append(" ".join(str(c) for c in row if c is not None))
        wb.close()
        return "\n".join(chunks)[:5000]
    except Exception as exc:  # noqa: BLE001
        return f"[sheet-error: {exc}]"


def read_docx(p: Path) -> str:
    try:
        import docx
        return "\n".join(par.text for par in docx.Document(str(p)).paragraphs)[:5000]
    except Exception as exc:  # noqa: BLE001
        return f"[docx-error: {exc}]"


def read_image_ocr(p: Path, timeout: int = 60) -> str:
    """Deterministic, token-free OCR via tesseract. Returns extracted text."""
    if not TESSERACT:
        return "[no-tesseract]"
    try:
        langs = os.environ.get("TESSERACT_LANGS", "spa+eng")
        env = dict(os.environ)
        if TESSDATA_DIR.exists():
            env["TESSDATA_PREFIX"] = str(TESSDATA_DIR)
        r = subprocess.run([TESSERACT, str(p), "stdout", "-l", langs], capture_output=True,
                           text=True, timeout=timeout, encoding="utf-8", errors="ignore", env=env)
        if r.returncode != 0 and "spa" in langs:  # spa unavailable -> retry eng only
            r = subprocess.run([TESSERACT, str(p), "stdout"], capture_output=True,
                               text=True, timeout=timeout, encoding="utf-8", errors="ignore")
        return r.stdout or ""
    except Exception as exc:  # noqa: BLE001
        return f"[ocr-error: {exc}]"


OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")
_AI_PROMPT = (
    "Mira este archivo adjunto. ¿Es un comprobante de pago o transferencia bancaria "
    "(no un pasaporte, rooming list ni itinerario)? Responde SOLO un objeto JSON: "
    '{"is_receipt": true|false, "amount": number|null, "currency": string|null, '
    '"date": "YYYY-MM-DD"|null}.'
)
_TOKENS = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}


def read_image_openai(p: Path, model: str = None, timeout: int = 120) -> dict:
    """Vision read via OpenAI (default gpt-5.6-luna). Accumulates token usage in _TOKENS."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return {"is_receipt": None, "amount": None, "date": None, "notes": "no OPENAI_API_KEY"}
    model = model or OPENAI_MODEL
    ext = p.suffix.lower().lstrip(".").replace("jpg", "jpeg") or "jpeg"
    try:
        b64 = base64.b64encode(p.read_bytes()).decode()
        body = {"model": model, "messages": [{"role": "user", "content": [
            {"type": "text", "text": _AI_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/{ext};base64,{b64}"}}]}]}
        r = requests.post(OPENAI_URL, headers={"Authorization": f"Bearer {key}",
                          "Content-Type": "application/json"}, json=body, timeout=timeout)
        if r.status_code != 200:
            return {"is_receipt": None, "amount": None, "date": None, "notes": f"openai {r.status_code}: {r.text[:100]}"}
        d = r.json()
        u = d.get("usage", {})
        _TOKENS["prompt"] += u.get("prompt_tokens", 0)
        _TOKENS["completion"] += u.get("completion_tokens", 0)
        _TOKENS["total"] += u.get("total_tokens", 0)
        _TOKENS["calls"] += 1
        m = re.search(r"\{.*\}", d["choices"][0]["message"]["content"], re.S)
        return json.loads(m.group(0)) if m else {"is_receipt": None, "amount": None, "date": None, "notes": "no-json"}
    except Exception as exc:  # noqa: BLE001
        return {"is_receipt": None, "amount": None, "date": None, "notes": f"openai-error: {exc}"}


def read_image_cli(p: Path, timeout: int = 120) -> dict:
    """Use the `claude` CLI as a vision reader. Returns {is_receipt, amount, date, notes}."""
    prompt = (
        "Look at the attached image file and decide if it is a payment receipt "
        "(comprobante de pago o transferencia bancaria), as opposed to a passport, "
        "rooming list, ID, or itinerary. Reply with ONLY a JSON object: "
        '{"is_receipt": true|false, "amount": number|null, "currency": string|null, '
        '"date": "YYYY-MM-DD"|null, "notes": string}. Image path: ' + str(p)
    )
    try:
        r = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8")
        out = r.stdout or ""
        m = re.search(r"\{.*\}", out, re.S)
        if m:
            return json.loads(m.group(0))
        return {"is_receipt": None, "amount": None, "date": None, "notes": "no-json:" + out[:120]}
    except Exception as exc:  # noqa: BLE001
        return {"is_receipt": None, "amount": None, "date": None, "notes": f"cli-error: {exc}"}


# ---------------------------------------------------------------- classify
def parse_amount(s: str) -> float | None:
    s = s.replace(" ", "")
    # Decimal separator is the last '.' or ',' in the string; the other groups thousands.
    last_dot, last_comma = s.rfind("."), s.rfind(",")
    if last_dot == -1 and last_comma == -1:
        norm = s
    elif last_comma > last_dot:  # comma is decimal (Argentine)
        norm = s.replace(".", "").replace(",", ".")
    else:  # dot is decimal (US)
        norm = s.replace(",", "")
    try:
        return round(float(norm), 2)
    except ValueError:
        return None


def classify_from_text(res: dict, name: str, text: str) -> dict:
    """Deterministic classification. Strong phrases and Argentine structural markers
    (CUIT/CBU/CVU) override the not-receipt gate; weak words only count when not gated."""
    blob = f"{name}\n{text}"
    strong = bool(STRONG_WORDS.search(blob))
    struct = has_structural(blob)
    weak = bool(RECEIPT_WORDS.search(blob))
    gated_out = bool(NOT_RECEIPT_WORDS.search(blob))
    res["is_receipt"] = bool(strong or struct or (weak and not gated_out))
    res["signal"] = "strong" if strong else "struct" if struct else "weak" if (weak and not gated_out) else "none"
    if res["is_receipt"]:
        m = AMOUNT_RE.search(text) or AMOUNT_FALLBACK.search(text)
        if m:
            res["amount"] = parse_amount(m.group(1))
            res["currency"] = "ARS" if "$" in text else None
        dm = DATE_RE.search(text)
        if dm:
            res["date"] = dm.group(0)
    res["notes"] = re.sub(r"\s+", " ", text)[:160]
    return res


def classify_file(name: str, path: Path, use_llm: bool = False, skip_images: bool = False) -> dict:
    ext = path.suffix.lower()
    res = {"file": name, "kind": ext.lstrip("."), "is_receipt": None, "signal": "",
           "amount": None, "currency": None, "date": None, "how": "", "notes": ""}
    if ext in PDF_EXT:
        return classify_from_text({**res, "how": "pdf-text"}, name, read_pdf(path))
    if ext in SHEET_EXT:
        return classify_from_text({**res, "how": "sheet"}, name, read_sheet(path))
    if ext in DOC_EXT:
        return classify_from_text({**res, "how": "docx"}, name, read_docx(path))
    if ext in IMG_EXT:
        if skip_images:
            res["how"] = "image-skipped"; res["notes"] = "needs OCR"; return res
        if use_llm:  # opt-in, uses tokens
            v = read_image_cli(path); res["how"] = "cli-vision"
            res.update({k: v.get(k) for k in ("is_receipt", "amount", "currency", "date") if k in v})
            res["notes"] = str(v.get("notes", ""))[:200]
            return res
        return classify_from_text({**res, "how": "ocr"}, name, read_image_ocr(path))  # deterministic, free
    res["how"] = "unknown-ext"
    return res


# ---------------------------------------------------------------- veracidad
def build_expected(targets: list[dict], api_key: str) -> dict[str, dict[str, float]]:
    """cid -> {currency: expected_amount}, from booking/list rows (reliable), by checkout day."""
    days = sorted({t.get("checkout") for t in targets if t.get("checkout")})
    exp: dict[str, dict[str, float]] = {}
    for day in days:
        try:
            for b in ca.fetch_checkouts(api_key, day):
                bid = str(b.get("booking_id") or b.get("id") or "")
                if bid:
                    exp[bid] = ca.room_budgets(b)
        except Exception:  # noqa: BLE001
            continue
    return exp


class RateProvider:
    """Per-day USD->ARS from BCRA (v4.0, variable 4). Nearest prior day for
    weekends/holidays; falls back to USD_ARS_RATE only if BCRA is unreachable."""

    BASE = "https://api.bcra.gob.ar/estadisticas/v4.0/monetarias/{vid}"

    def __init__(self, start: str, end: str):
        self.rates: dict[str, float] = {}
        self.fallback = float(os.environ.get("USD_ARS_RATE") or 0) or None
        vid = int(os.environ.get("BCRA_USD_ARS_VARIABLE_ID") or 4)
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            r = requests.get(self.BASE.format(vid=vid),
                             params={"desde": start, "hasta": end}, timeout=30, verify=False)
            r.raise_for_status()
            results = r.json().get("results")
            if isinstance(results, dict):
                results = [results]
            for item in results or []:
                for row in (item.get("detalle") or ([item] if "fecha" in item else [])):
                    d = str(row.get("fecha") or "")[:10]
                    v = str(row.get("valor") or "").replace(",", ".")
                    try:
                        fv = float(v)
                    except ValueError:
                        continue
                    if d and fv > 0:
                        self.rates[d] = fv
        except Exception as exc:  # noqa: BLE001
            log(f"[rate] BCRA fetch failed ({exc}); using fallback USD_ARS_RATE={self.fallback}")

    def get(self, day: str | None) -> float | None:
        if not self.rates:
            return self.fallback
        if not day:
            day = max(self.rates)
        day = day[:10]
        # exact, else nearest previous available date
        keys = sorted(k for k in self.rates if k <= day)
        if keys:
            return self.rates[keys[-1]]
        return self.rates[min(self.rates)] if self.rates else self.fallback


def _norm_date(s: str | None) -> str | None:
    if not s:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{2})[/-](\d{2})[/-](\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


def veracidad(receipt_amount, expected: dict[str, float], rate: float | None, tol: float = 0.20) -> tuple[str, str]:
    """Compare a receipt amount to the expected reservation amount (FX-aware, currency-agnostic)."""
    if not expected:
        return "SIN-ESPERADO", ""
    if not rate:
        return "SIN-TC", ", ".join(f"{c} {a:,.2f}" for c, a in expected.items())
    # Sum all currencies to USD (a group may mix USD and ARS bookings).
    exp_usd = expected.get("USD", 0) + expected.get("ARS", 0) / rate
    for c, a in expected.items():
        if c not in ("USD", "ARS"):
            exp_usd += a  # unknown currency: treat as USD-ish rather than drop
    exp_txt = ", ".join(f"{c} {a:,.2f}" for c, a in expected.items())
    if not exp_usd:
        return "SIN-ESPERADO", exp_txt
    if receipt_amount in (None, ""):
        return "MONTO-ILEGIBLE", f"esperado {exp_txt}"
    amt = float(receipt_amount)
    # Try the receipt as USD and as ARS; accept if either lands within tolerance.
    cand = {"USD": amt, "ARS->USD": amt / rate}
    best_lbl, best_ratio = None, None
    for lbl, usd in cand.items():
        ratio = usd / exp_usd if exp_usd else 0
        if best_ratio is None or abs(ratio - 1) < abs(best_ratio - 1):
            best_lbl, best_ratio = lbl, ratio
    detail = f"esperado {exp_txt} | recibo {amt:,.2f} ({best_lbl}) ratio {best_ratio:.2f}"
    if abs(best_ratio - 1) <= tol:
        return "OK-MONTO", detail
    return "REVISAR-MONTO", detail


# ---------------------------------------------------------------- driver
def load_targets(args) -> list[dict]:
    if args.cid:
        return [{"cid": args.cid, "checkout": "", "guest": "", "channel": "", "api_verdict": ""}]
    if args.from_csv:
        rows = list(csv.DictReader(open(args.from_csv, encoding="utf-8")))
        return [r for r in rows if r.get("archivos") == "con_archivos"]
    # else build from API
    api_key = os.environ["PXSOL_API_KEY"]
    days = [ac.dt.datetime.now(ac.TZ).date().fromordinal(
        ac.dt.datetime.now(ac.TZ).date().toordinal() - i).isoformat() for i in range(1, args.days + 1)]
    return ac.build_worklist(api_key, days)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="from_csv", help="archivos CSV; use its con_archivos rows")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--cid")
    ap.add_argument("--use-llm", action="store_true", help="read images with the claude CLI (uses tokens); default is local OCR")
    ap.add_argument("--skip-images", action="store_true", help="do not read image files at all")
    ap.add_argument("--ai-fallback", action="store_true", help="use OpenAI vision only on images the deterministic path can't resolve")
    ap.add_argument("--ai-model", default=OPENAI_MODEL, help=f"OpenAI vision model (default {OPENAI_MODEL})")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    targets = load_targets(args)
    if args.limit:
        targets = targets[: args.limit]
    log(f"{len(targets)} reservations with files to read")

    # Expected amounts (from booking/list) and a per-day BCRA USD->ARS rate for veracidad.
    api_key = os.environ.get("PXSOL_API_KEY", "")
    expected_map = build_expected(targets, api_key) if api_key else {}
    days_present = sorted({t.get("checkout")[:10] for t in targets if t.get("checkout")})
    rates = RateProvider(days_present[0], days_present[-1]) if days_present else RateProvider("", "")

    sess = ac.ensure_http_session(STATE, targets[0]["cid"] if targets else None)
    file_rows = []
    read_cache: dict[str, dict] = {}   # sha256 -> classification (read each unique file once)
    collected = []                     # per-reservation raw data for the reconcile pass

    # Pass 1: fetch, download, classify. Dedup reading by content hash.
    for i, t in enumerate(targets, 1):
        cid = t["cid"]
        try:
            r = sess.get(ac.FILES_URL.format(cid=cid, t=int(time.time() * 1000)), timeout=30)
            files = extract_files(r.text)
        except Exception as exc:  # noqa: BLE001
            log(f"[{i}/{len(targets)}] {cid} FETCH ERROR {exc}")
            continue
        receipts = []
        for name, url in files:
            try:
                p = download(sess, cid, name, url)
                h = sha256(p)
                if h in read_cache:
                    c = dict(read_cache[h])  # identical bytes already read elsewhere
                else:
                    c = classify_file(name, p, use_llm=args.use_llm, skip_images=args.skip_images)
                    # AI fallback: only on images the deterministic path could not resolve
                    # (no receipt detected, or receipt with no readable amount). Uses tokens.
                    if args.ai_fallback and c["kind"] in ("jpg", "jpeg", "png", "webp") \
                            and (not c.get("is_receipt") or c.get("amount") in (None, "")):
                        v = read_image_openai(p, model=args.ai_model)
                        if v.get("is_receipt") is not None:
                            c["is_receipt"] = bool(v.get("is_receipt"))
                            c["signal"] = "ai"
                            c["how"] = f"{c['how']}+ai"
                            if v.get("amount") not in (None, ""):
                                c["amount"] = v.get("amount")
                                c["currency"] = v.get("currency") or c.get("currency")
                            if v.get("date"):
                                c["date"] = v.get("date")
                    read_cache[h] = dict(c)
                c["hash"] = h
            except Exception as exc:  # noqa: BLE001
                c = {"file": name, "kind": "?", "is_receipt": None, "amount": None, "currency": None,
                     "date": None, "how": "error", "notes": str(exc)[:120], "hash": ""}
            c["cid"] = cid; c["file"] = name
            file_rows.append(c)
            if c.get("is_receipt"):
                receipts.append(c)
        best = max(receipts, key=lambda x: (x.get("amount") or 0), default=None)
        collected.append({"t": t, "cid": cid, "files": files, "receipts": receipts, "best": best})
        log(f"[{i}/{len(targets)}] {cid} files={len(files)} rcpt={len(receipts)} "
            f"{'best=' + str(best.get('amount')) if best else ''}")

    # Bulk grouping: reservations that share the exact same receipt file (same sha256).
    hash_to_cids: dict[str, set] = {}
    for c in collected:
        if c["best"] and c["best"].get("hash"):
            hash_to_cids.setdefault(c["best"]["hash"], set()).add(c["cid"])

    # Pass 2: reconcile. A shared receipt is a bulk transfer -> compare its amount to the
    # SUM of expected across every reservation that shares it.
    res_rows = []
    for c in collected:
        t, cid, best = c["t"], c["cid"], c["best"]
        own_expected = expected_map.get(cid, {})
        r_amount = best["amount"] if best else None
        fx_day = (_norm_date(best.get("date")) if best else None) or (t.get("checkout") or "")[:10]
        rate = rates.get(fx_day)
        group = sorted(hash_to_cids.get(best["hash"], {cid})) if best else [cid]
        if len(group) > 1:  # bulk: expected is the sum over the group
            expected = {}
            for gc in group:
                for cur, amt in expected_map.get(gc, {}).items():
                    expected[cur] = expected.get(cur, 0) + amt
        else:
            expected = own_expected
        if not c["receipts"]:
            match_flag, match_detail = "SIN-COMPROBANTE", ""
        else:
            match_flag, match_detail = veracidad(r_amount, expected, rate)
            if len(group) > 1:
                match_flag = "OK-BULK" if match_flag == "OK-MONTO" else match_flag
                match_detail = f"bulk x{len(group)} | " + match_detail
        res_rows.append({
            "cid": cid, "checkout": t.get("checkout", ""), "guest": t.get("guest", ""),
            "channel": t.get("channel", ""), "api_verdict": t.get("api_verdict", ""),
            "n_files": len(c["files"]), "n_receipts": len(c["receipts"]), "has_receipt": bool(c["receipts"]),
            "receipt_amount": r_amount, "receipt_currency": best.get("currency") if best else None,
            "receipt_date": best["date"] if best else None,
            "receipt_hash": best["hash"][:12] if best and best.get("hash") else "",
            "bulk_size": len(group), "bulk_cids": " ".join(group) if len(group) > 1 else "",
            "expected": ", ".join(f"{cur} {a:,.2f}" for cur, a in expected.items()),
            "fx_date": fx_day, "fx_rate": rate, "match_flag": match_flag, "match_detail": match_detail,
            "files": " | ".join(n for n, _ in c["files"]),
        })
    log(f"read {len(read_cache)} unique files (of {len(file_rows)} total); "
        f"{sum(1 for v in hash_to_cids.values() if len(v) > 1)} shared receipts")

    fcsv = OUT / "comprobantes_files.csv"
    with fcsv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["cid", "file", "kind", "is_receipt", "signal", "amount",
                                           "currency", "date", "how", "hash", "notes"], extrasaction="ignore")
        w.writeheader(); w.writerows(file_rows)
    rcsv = OUT / "comprobantes.csv"
    with rcsv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(res_rows[0].keys()) if res_rows else ["cid"])
        w.writeheader(); w.writerows(res_rows)
    with_r = sum(1 for r in res_rows if r["has_receipt"])
    log(f"\n{len(res_rows)} reservations read; {with_r} have a receipt, "
        f"{len(res_rows) - with_r} have files but no detected receipt.")
    if _TOKENS["calls"]:
        # Rough cost estimate; adjust rates to gpt-5.6-luna's actual pricing if different.
        rate_in = float(os.environ.get("OPENAI_IN_PER_MTOK", "0.10"))
        rate_out = float(os.environ.get("OPENAI_OUT_PER_MTOK", "0.40"))
        cost = _TOKENS["prompt"] / 1e6 * rate_in + _TOKENS["completion"] / 1e6 * rate_out
        log(f"AI fallback: {_TOKENS['calls']} calls, {_TOKENS['total']} tokens "
            f"(prompt {_TOKENS['prompt']}, completion {_TOKENS['completion']}); "
            f"est ~${cost:.4f} at ${rate_in}/${rate_out} per Mtok")
    log(f"CSV: {rcsv}  (per-file: {fcsv})")
    return 0


if __name__ == "__main__":
    ac._load_dotenv()
    sys.exit(main())
