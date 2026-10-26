import io
import logging
import math
import re
import threading
from statistics import median

from flask import Blueprint, jsonify, request

from .common import err, login_required
from .currency import CODES

bp = Blueprint("ocr", __name__)


def _to_number(tok):
    last = max(tok.rfind("."), tok.rfind(","))
    if last == -1:
        return float(tok)
    frac = tok[last + 1:]
    if len(frac) == 2 or (tok.count(tok[last]) == 1 and len(frac) != 3):
        return float(re.sub(r"[.,]", "", tok[:last]) + "." + frac)
    return float(re.sub(r"[.,]", "", tok))


_DIGIT_LOOKALIKES = str.maketrans({"O": "0", "o": "0", "D": "0", "S": "5", "s": "5", "I": "1", "l": "1", "|": "1", "B": "8"})


def _fix_digits(line):
    def fix(m):
        tok = m.group(0)
        return tok.translate(_DIGIT_LOOKALIKES) if re.search(r"\d", tok) and re.search(r"[.,]", tok) else tok
    return re.sub(r"[\dODSIlsoB|][\dODSIlsoB|.,]*[\dODSIlsoB|]", fix, line)


_TOTAL = re.compile(r"total|amount\s*(due|payable)|balance\s*due|to\s*pay|gesamt|summe|montant|importe|totaal|合計|合计", re.I)
_NOT_TOTAL = re.compile(r"sub\s*-?\s*tot|zwischen|total\s*(items?|qty|quantity|savings?|discount|tax|vat|tva|iva)\b", re.I)


def parse_receipt_text(text):
    symbols = {"€": "EUR", "$": "USD", "£": "GBP", "₹": "INR", "¥": "JPY"}
    cur = None
    for s, c in symbols.items():
        if s in text:
            cur = c
            break
    for code in CODES:
        if re.search(rf"\b{code}\b", text):
            cur = code
            break
    total, source = None, None
    for line in text.splitlines():
        if _TOTAL.search(line) and not _NOT_TOTAL.search(line):
            nums = re.findall(r"\d[\d.,]*\d|\d", _fix_digits(line))
            if nums:
                total, source = _to_number(nums[-1]), "total-line"
    if total is None:
        nums = [_to_number(n) for n in re.findall(r"\d[\d.,]*[.,]\d{2}\b", _fix_digits(text))]
        total, source = (max(nums), "largest-amount") if nums else (None, None)
    return {"amount": total, "currency": cur, "found_by": source, "items": receipt_items(text),
            "merchant": receipt_merchant(text), "date": receipt_date(text, cur), "category": guess_category(text),
            "text": text.strip()[:500]}


_NOT_ITEM = re.compile(r"total|tot\b|sum|gesamt|montant|importe|amount|balance|tax|vat|iva|tva|mwst|gst|service|servizio|"
                       r"tip|mancia|coperto|cover|discount|sconto|rabatt|change|resto|cash|contanti|card|carta|visa|master|"
                       r"amex|credit|debit|paid|tendered|rounding|due|pay", re.I)
_PRICE_AT_END = re.compile(r"^(.*?[A-Za-zÀ-ÿ].*?)[\s:.\-]*[$€£₹¥]?\s*(-?\d[\d.,]*[.,]\d{2})\s*(?:[A-Z]{3}|[$€£₹¥])?\s*$")


_GARBLED_PRICE = re.compile(r"^(.*?[A-Za-zÀ-ÿ].*?)\s+[$€£₹¥]?([\d.,]{2,})\s*$")


def receipt_items(text):
    items, unreadable, subtotal = [], [], None
    for line in text.splitlines():
        line = _fix_digits(line.strip())
        is_sub = re.search(r"sub\s*-?\s*tot|zwischen", line, re.I)
        if is_sub or (_TOTAL.search(line) and not _NOT_TOTAL.search(line)):
            nums = re.findall(r"\d[\d.,]*[.,]\d{2}", line)
            subtotal = _to_number(nums[-1]) if is_sub and nums else None
            break
        m = _PRICE_AT_END.match(line)
        if m and not _NOT_ITEM.search(m.group(1)):
            name, price = m.group(1).strip(" .:-*"), _to_number(m.group(2))
            if len(re.findall(r"[A-Za-zÀ-ÿ]", name)) >= 2 and 0 < price < 100000:
                items.append({"name": name[:60], "price": round(price, 2)})
            continue
        g = _GARBLED_PRICE.match(line)
        if items and g and not _NOT_ITEM.search(g.group(1)) and len(re.findall(r"[A-Za-zÀ-ÿ]", g.group(1))) >= 2:
            unreadable.append({"name": g.group(1).strip(" .:-*")[:60], "price": None})
            items.append(unreadable[-1])
    if len(unreadable) == 1 and subtotal:
        missing = round(subtotal - sum(it["price"] for it in items if it["price"]), 2)
        if missing > 0:
            unreadable[0]["price"] = missing
    return items[:40]


def receipt_merchant(text):
    for line in text.splitlines()[:5]:
        line = line.strip()
        if len(re.findall(r"[A-Za-zÀ-ÿ]", line)) >= 3 and not re.search(r"\d{2}[./-]\d{2}|\d[.,]\d{2}|tel|phone|www|@", line, re.I):
            return line.title() if line.isupper() else line
    return None


_MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def receipt_date(text, currency=None):
    from datetime import date, timedelta
    candidates = []
    for y, mo, d in re.findall(r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b", text):
        candidates.append((int(y), int(mo), int(d)))
    for a, b, y in re.findall(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})\b", text):
        a, b, y = int(a), int(b), int(y) + (2000 if len(y) == 2 else 0)
        month_first = b > 12 or (a <= 12 and currency == "USD")
        candidates.append((y, a, b) if month_first else (y, b, a))
    for d, mon, y in re.findall(r"\b(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})\b", text):
        if mon.lower() in _MONTHS:
            candidates.append((int(y), _MONTHS[mon.lower()], int(d)))
    for mon, d, y in re.findall(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b", text):
        if mon.lower() in _MONTHS:
            candidates.append((int(y), _MONTHS[mon.lower()], int(d)))
    today = date.today()
    for y, mo, d in candidates:
        try:
            found = date(y, mo, d)
        except ValueError:
            continue
        if today - timedelta(days=730) <= found <= today + timedelta(days=1):
            return found.isoformat()
    return None


_CATEGORY_WORDS = [
    ("Accommodation", r"hotel|hostel|motel|\binn\b|resort|airbnb|b&b|guest\s*house|albergo|pension"),
    ("Transport", r"taxi|uber|lyft|bolt|train|rail|metro|subway|\bbus\b|airline|airways|flight|fuel|petrol|diesel|"
                  r"parking|toll|ferry|car\s*hire|rental"),
    ("Entertainment", r"museum|museo|cinema|theat(re|er)|ticket|biglietto|\btour\b|concert|\bzoo\b|gallery|admission"),
    ("Food", r"restaurant|ristorante|trattoria|osteria|pizz|caf[eé]|caff[eè]|coffee|\bbar\b|bistro|\bdeli|bakery|"
             r"burger|kitchen|grill|sushi|gelat|dinner|lunch|breakfast|supermarket|grocery|market|food|wine|vino|beer"),
    ("Shopping", r"\bshop\b|store|boutique|\bmall\b|souvenir|pharmacy|farmacia|apotheke|clothing|fashion"),
]


def guess_category(text):
    for category, words in _CATEGORY_WORDS:
        if re.search(words, text, re.I):
            return category
    return "Other"


def _otsu_threshold(gray):
    hist = gray.histogram()
    total = sum(hist)
    sum_all = sum(i * h for i, h in enumerate(hist))
    best, best_t, w_b, sum_b = 0.0, 127, 0, 0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0 or w_b == total:
            continue
        sum_b += t * hist[t]
        m_b, m_f = sum_b / w_b, (sum_all - sum_b) / (total - w_b)
        between = w_b * (total - w_b) * (m_b - m_f) ** 2
        if between > best:
            best, best_t = between, t
    return best_t


def prepare_image(img):
    from PIL import ImageFilter, ImageOps
    img = ImageOps.exif_transpose(img)
    gray = img.convert("L")
    if gray.width < 1600:
        scale = min(3.0, 1600 / max(gray.width, 1))
        gray = gray.resize((int(gray.width * scale), int(gray.height * scale)), resample=3)
    gray = ImageOps.autocontrast(gray, cutoff=2)
    gray = gray.filter(ImageFilter.MedianFilter(3))
    t = _otsu_threshold(gray)
    return gray, gray.point(lambda p: 255 if p > t else 0)


def boxes_to_lines(boxes, texts):
    items, angles, heights = [], [], []
    for box, text in zip(boxes, texts):
        (x0, y0), (x1, y1), (x2, y2), (x3, y3) = [tuple(map(float, p)) for p in box[:4]]
        if x1 - x0 > 15:
            angles.append(math.atan2(y1 - y0, x1 - x0))
        heights.append(max(abs((y3 + y2) / 2 - (y0 + y1) / 2), 1.0))
        items.append(((x0 + x1 + x2 + x3) / 4, (y0 + y1 + y2 + y3) / 4, text))
    if not items:
        return ""
    a = median(angles) if angles else 0.0
    sin_a, cos_a = math.sin(a), math.cos(a)
    straight = sorted(((cos_a * cy - sin_a * cx, cos_a * cx + sin_a * cy, t) for cx, cy, t in items))
    tol = 0.55 * median(heights)
    rows = []
    for y, x, t in straight:
        if rows and abs(y - rows[-1]["y"]) <= tol:
            row = rows[-1]
            row["items"].append((x, t))
            row["y"] += (y - row["y"]) / len(row["items"])
        else:
            rows.append({"y": y, "items": [(x, t)]})
    return "\n".join("  ".join(t for _, t in sorted(r["items"])) for r in rows)


_rapid, _rapid_lock = None, threading.Lock()


def _rapid_engine():
    global _rapid
    if _rapid is None:
        with _rapid_lock:
            if _rapid is None:
                from rapidocr import RapidOCR
                logging.getLogger("RapidOCR").setLevel(logging.WARNING)
                _rapid = RapidOCR()
    return _rapid


def _read_with_rapidocr(img):
    engine = _rapid_engine()
    with _rapid_lock:
        out = engine(img)
    if out is None or not getattr(out, "txts", None):
        return None
    res = parse_receipt_text(boxes_to_lines(out.boxes, out.txts))
    res["engine"] = "rapidocr"
    return res


def _read_with_tesseract(img):
    import pytesseract
    gray, bw = prepare_image(img)
    best = None
    for image, mode in ((bw, "--oem 3 --psm 4"), (gray, "--oem 3 --psm 6")):
        res = parse_receipt_text(pytesseract.image_to_string(image, config=mode))
        res["engine"] = "tesseract"
        if res["found_by"] == "total-line":
            return res
        if best is None or (best["amount"] is None and res["amount"] is not None):
            best = res
    return best


def run_ocr(file_bytes):
    try:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(file_bytes))).convert("RGB")
    except Exception:
        return None
    if max(img.size) > 1600:
        img.thumbnail((1600, 1600))
    best = None
    for reader in (_read_with_rapidocr, _read_with_tesseract):
        try:
            res = reader(img)
        except Exception:
            continue
        if res and res["found_by"] == "total-line":
            return res
        if res and (best is None or (best["amount"] is None and res["amount"] is not None)):
            best = res
    return best


@bp.post("/api/ocr")
@login_required
def ocr_api():
    f = request.files.get("receipt")
    if not f:
        return err("Attach a receipt image.")
    res = run_ocr(f.read())
    if not res or res["amount"] is None:
        return err("Could not read the receipt automatically. Please enter the details manually.", 422)
    return jsonify(res)
