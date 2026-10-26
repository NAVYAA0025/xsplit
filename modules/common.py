import math
from datetime import date
from functools import wraps

from flask import jsonify, request, session

from .db import get_db

MAX_AMOUNT = 1_000_000_000


def err(msg, code=400):
    return jsonify({"error": msg}), code


def body():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if "uid" not in session:
            return err("Please log in.", 401)
        return fn(*a, **kw)
    return wrapper


def money(x):
    return round(float(x) + 1e-9, 2)


def positive_number(value, label="Amount"):
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Enter a valid {label.lower()}.")
    if not math.isfinite(x) or x <= 0:
        raise ValueError(f"{label} must be greater than zero.")
    if x > MAX_AMOUNT:
        raise ValueError(f"{label} is too large.")
    return x


def iso_date(value, label="Date"):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ValueError(f"{label} must be a valid date (YYYY-MM-DD).")


def member_row(gid):
    return get_db().execute(
        "SELECT * FROM group_member WHERE group_id=? AND user_id=? AND active=1", (gid, session["uid"])).fetchone()


def group_or_error(gid):
    grp = get_db().execute("SELECT * FROM grp WHERE group_id=?", (gid,)).fetchone()
    if not grp:
        return None, err("Group not found.", 404)
    if not member_row(gid):
        return None, err("You do not have access to this group.", 403)
    return grp, None


def is_admin(gid):
    m = member_row(gid)
    return bool(m and m["role"] == "Admin")


def member_name(m):
    if m["is_guest"]:
        return m["guest_name"] + " (guest)"
    u = get_db().execute("SELECT full_name FROM user WHERE user_id=?", (m["user_id"],)).fetchone()
    return u["full_name"]
