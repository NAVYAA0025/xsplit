import json
import re
import time
import urllib.request

from flask import Blueprint, current_app, jsonify, request

from .common import err, login_required

bp = Blueprint("currency", __name__)

CURRENCIES = [
    ("USD", "US Dollar", "$"), ("EUR", "Euro", "€"), ("GBP", "British Pound", "£"),
    ("INR", "Indian Rupee", "₹"), ("JPY", "Japanese Yen", "¥"), ("AUD", "Australian Dollar", "A$"),
    ("CAD", "Canadian Dollar", "C$"), ("CHF", "Swiss Franc", "CHF"), ("SGD", "Singapore Dollar", "S$"),
    ("AED", "UAE Dirham", "AED"), ("THB", "Thai Baht", "฿"),
]
CODES = [c[0] for c in CURRENCIES]
FALLBACK_PER_USD = {"USD": 1, "EUR": 0.92, "GBP": 0.79, "INR": 83.2, "JPY": 150.0, "AUD": 1.53,
                    "CAD": 1.36, "CHF": 0.9, "SGD": 1.35, "AED": 3.67, "THB": 36.0}
_rate_cache = {}
LATEST_RATE_TTL = 3600


def get_rate(frm, to, on=None):
    if frm == to:
        return 1.0, "same-currency"
    if on and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", on):
        on = None
    key = (frm, to, on or "latest")
    hit = _rate_cache.get(key)
    if hit and (on or time.time() - hit[1] < LATEST_RATE_TTL):
        return hit[0], "cached"
    if not current_app.config["TESTING"] and frm in CODES and to in CODES:
        try:
            url = f"https://api.frankfurter.app/{on or 'latest'}?from={frm}&to={to}"
            with urllib.request.urlopen(url, timeout=4) as r:
                rate = json.load(r)["rates"][to]
            _rate_cache[key] = (float(rate), time.time())
            return float(rate), "live"
        except Exception:
            pass
    if frm in FALLBACK_PER_USD and to in FALLBACK_PER_USD:
        return FALLBACK_PER_USD[to] / FALLBACK_PER_USD[frm], "fallback"
    raise ValueError("Exchange rate unavailable.")


@bp.get("/api/rate")
@login_required
def rate_api():
    try:
        r, src = get_rate(request.args["from"].upper(), request.args["to"].upper(), request.args.get("date"))
        return jsonify({"rate": r, "source": src})
    except (KeyError, ValueError):
        return err("Exchange rate service unavailable. Enter a manual rate.", 503)
