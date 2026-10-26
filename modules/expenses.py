import io
import json
import os
import secrets
from datetime import date

from flask import Blueprint, Response, current_app, jsonify, request, send_from_directory, session
from werkzeug.utils import secure_filename

from .common import err, group_or_error, iso_date, login_required, member_name, member_row, money, positive_number
from .currency import CODES, get_rate
from .db import get_db
from .splits import build_item_splits, build_splits, clean_items

bp = Blueprint("expenses", __name__)

CATEGORIES = ["Food", "Transport", "Accommodation", "Shopping", "Entertainment", "Other"]
RECEIPT_TYPES = (".jpg", ".jpeg", ".png", ".pdf", ".webp")


def expense_dict(e):
    db = get_db()
    splits = db.execute("""SELECT s.*, m.guest_name, m.is_guest, m.user_id FROM expense_split s
                           JOIN group_member m ON m.member_id=s.member_id WHERE expense_id=?""", (e["expense_id"],)).fetchall()
    payer = db.execute("SELECT * FROM group_member WHERE member_id=?", (e["paid_by"],)).fetchone()
    rec = db.execute("SELECT file_url FROM receipt WHERE expense_id=?", (e["expense_id"],)).fetchone()
    d = dict(e)
    d["paid_by_name"] = member_name(payer)
    d["receipt_url"] = rec["file_url"] if rec else None
    d["split_type"] = splits[0]["split_type"] if splits else "equal"
    d["splits"] = [{"member_id": s["member_id"], "name": member_name(s), "share_base": s["share_amount_base"],
                    "share_original": s["share_amount_original"], "percentage": s["share_percentage"]} for s in splits]
    d["items"] = [{"name": i["name"], "price": i["price"], "members": [int(m) for m in i["members"].split(",") if m]}
                  for i in db.execute("SELECT * FROM expense_item WHERE expense_id=? ORDER BY item_id", (e["expense_id"],))]
    return d


def parse_expense_payload():
    if request.content_type and request.content_type.startswith("multipart/"):
        d = json.loads(request.form.get("data", "{}"))
        return (d if isinstance(d, dict) else {}), request.files.get("receipt")
    d = request.get_json(silent=True)
    return (d if isinstance(d, dict) else {}), None


def validate_and_build(grp, d, default_date=None):
    gid = grp["group_id"]
    db = get_db()
    description = (d.get("description") or "").strip()
    if not description:
        raise ValueError("Enter a description.")
    if len(description) > 120:
        raise ValueError("Description is too long (max 120 characters).")
    amount = positive_number(d.get("amount"))
    cur = d.get("currency")
    if cur not in CODES:
        raise ValueError("Choose a supported currency.")
    if d.get("category", "Other") not in CATEGORIES:
        raise ValueError("Invalid category.")
    exp_date = iso_date(d.get("expense_date") or default_date or date.today().isoformat(), "Expense date")
    payer = db.execute("SELECT * FROM group_member WHERE member_id=? AND group_id=? AND active=1", (d.get("paid_by"), gid)).fetchone()
    if not payer:
        raise ValueError("Select a valid payer from the group.")
    valid_ids = {r["member_id"] for r in db.execute("SELECT member_id FROM group_member WHERE group_id=? AND active=1", (gid,))}
    items = clean_items(d.get("items"), valid_ids) if d.get("split_type") == "item" else []
    parts = d.get("participants") or []
    if not isinstance(parts, list) or not all(isinstance(p, dict) and "member_id" in p for p in parts):
        raise ValueError("Invalid participant list.")
    if any(p["member_id"] not in valid_ids for p in parts):
        raise ValueError("A participant is not in this group.")
    if cur == grp["base_currency"]:
        rate, src = 1.0, "same-currency"
    elif d.get("manual_rate"):
        rate, src = positive_number(d["manual_rate"], "Exchange rate"), "manual"
    else:
        rate, src = get_rate(cur, grp["base_currency"], exp_date if exp_date < date.today().isoformat() else None)
    if items:
        splits = build_item_splits(amount, rate, items)
    else:
        splits = build_splits(amount, rate, d.get("split_type", "equal"), parts)
    return description, amount, cur, exp_date, payer, rate, src, splits, items


def _upload_path(file_url):
    return os.path.join(current_app.config["UPLOAD_DIR"], os.path.basename(file_url))


RECEIPT_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".pdf": "application/pdf"}


def _shrink(data, ext):
    if ext == ".pdf":
        return data, RECEIPT_MIME[ext]
    try:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        img.thumbnail((2000, 2000))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=85)
        return (out.getvalue(), "image/jpeg") if out.tell() < len(data) else (data, RECEIPT_MIME[ext])
    except Exception:
        return data, RECEIPT_MIME[ext]


def _number_or_none(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def save_receipt(file, gid, expense_id, extracted=None):
    if not file or not file.filename:
        return
    ext = os.path.splitext(secure_filename(file.filename))[1].lower()
    if ext not in RECEIPT_TYPES:
        raise ValueError("Receipt must be a JPG, PNG, WEBP or PDF file.")
    extracted = extracted if isinstance(extracted, dict) else {}
    file.stream.seek(0)
    data, mime = _shrink(file.read(), ext)
    get_db().execute("""INSERT INTO receipt(group_id,expense_id,file_url,extracted_amount,extracted_currency,content,content_type)
                        VALUES(?,?,?,?,?,?,?)""",
                     (gid, expense_id, f"/uploads/{secrets.token_hex(8)}{ext}", _number_or_none(extracted.get("amount")),
                      extracted.get("currency") if extracted.get("currency") in CODES else None, data, mime))


def _remove_files(paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def _insert_splits(db, eid, splits, split_type, items=()):
    for mid, so, sb, pct in splits:
        db.execute("""INSERT INTO expense_split(expense_id,member_id,share_amount_base,share_percentage,share_amount_original,split_type)
                      VALUES(?,?,?,?,?,?)""", (eid, mid, sb, pct, so, split_type))
    for it in items:
        db.execute("INSERT INTO expense_item(expense_id,name,price,members) VALUES(?,?,?,?)",
                   (eid, it["name"], it["price"], ",".join(map(str, it["members"]))))


BAD_INPUT = (ValueError, KeyError, TypeError, json.JSONDecodeError)


@bp.post("/api/groups/<int:gid>/expenses")
@login_required
def add_expense(gid):
    grp, e = group_or_error(gid)
    if e:
        return e
    if grp["status"] == "Completed":
        return err("This group is closed.")
    db = get_db()
    try:
        d, file = parse_expense_payload()
        desc, amount, cur, exp_date, payer, rate, src, splits, items = validate_and_build(grp, d)
        db.execute("BEGIN")
        c = db.execute("""INSERT INTO expense(group_id,paid_by,description,category,amount,currency,expense_date,
                          exchange_rate_used,amount_base,rate_source) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                       (gid, payer["member_id"], desc, d.get("category", "Other"), amount, cur, exp_date, rate, money(amount * rate), src))
        _insert_splits(db, c.lastrowid, splits, d.get("split_type", "equal"), items)
        save_receipt(file, gid, c.lastrowid, d.get("ocr"))
        db.commit()
    except BAD_INPUT as ex:
        db.rollback()
        return err(str(ex) or "Invalid expense data.")
    return jsonify(expense_dict(db.execute("SELECT * FROM expense WHERE expense_id=?", (c.lastrowid,)).fetchone())), 201


@bp.get("/api/groups/<int:gid>/expenses")
@login_required
def list_expenses(gid):
    grp, e = group_or_error(gid)
    if e:
        return e
    rows = get_db().execute("SELECT * FROM expense WHERE group_id=? ORDER BY expense_date DESC, expense_id DESC", (gid,)).fetchall()
    return jsonify([expense_dict(r) for r in rows])


def editable_expense(eid):
    ex = get_db().execute("SELECT * FROM expense WHERE expense_id=?", (eid,)).fetchone()
    if not ex:
        return None, None, err("Expense not found.", 404)
    grp, e = group_or_error(ex["group_id"])
    if e:
        return None, None, e
    if grp["status"] == "Completed":
        return None, None, err("This group is closed.")
    me = member_row(ex["group_id"])
    if me["role"] != "Admin" and me["member_id"] != ex["paid_by"]:
        return None, None, err("Only the person who paid or the group creator can change this expense.", 403)
    return ex, grp, None


def _receipt_paths(db, eid):
    return [_upload_path(r["file_url"]) for r in db.execute("SELECT file_url FROM receipt WHERE expense_id=?", (eid,))]


@bp.put("/api/expenses/<int:eid>")
@login_required
def edit_expense(eid):
    ex, grp, e = editable_expense(eid)
    if e:
        return e
    db = get_db()
    old_files = []
    try:
        d, file = parse_expense_payload()
        desc, amount, cur, exp_date, payer, rate, src, splits, items = validate_and_build(grp, d, ex["expense_date"])
        if src == "manual" and cur == ex["currency"] and abs(rate - ex["exchange_rate_used"]) < 1e-9:
            src = ex["rate_source"]
        db.execute("BEGIN")
        db.execute("""UPDATE expense SET paid_by=?,description=?,category=?,amount=?,currency=?,expense_date=?,
                      exchange_rate_used=?,amount_base=?,rate_source=? WHERE expense_id=?""",
                   (payer["member_id"], desc, d.get("category", "Other"), amount, cur, exp_date, rate, money(amount * rate), src, eid))
        db.execute("DELETE FROM expense_split WHERE expense_id=?", (eid,))
        db.execute("DELETE FROM expense_item WHERE expense_id=?", (eid,))
        _insert_splits(db, eid, splits, d.get("split_type", "equal"), items)
        if file and file.filename:
            old_files = _receipt_paths(db, eid)
            db.execute("DELETE FROM receipt WHERE expense_id=?", (eid,))
            save_receipt(file, grp["group_id"], eid, d.get("ocr"))
        db.commit()
    except BAD_INPUT as ex2:
        db.rollback()
        return err(str(ex2) or "Invalid expense data.")
    _remove_files(old_files)
    return jsonify(expense_dict(db.execute("SELECT * FROM expense WHERE expense_id=?", (eid,)).fetchone()))


@bp.delete("/api/expenses/<int:eid>")
@login_required
def delete_expense(eid):
    ex, grp, e = editable_expense(eid)
    if e:
        return e
    db = get_db()
    files = _receipt_paths(db, eid)
    db.execute("DELETE FROM expense WHERE expense_id=?", (eid,))
    db.commit()
    _remove_files(files)
    return jsonify({"ok": True})


@bp.route("/uploads/<path:name>")
def uploads(name):
    token = request.args.get("guest")
    if not token and "uid" not in session:
        return err("Please log in.", 401)
    db = get_db()
    rec = db.execute("SELECT group_id, content, content_type FROM receipt WHERE file_url=?", (f"/uploads/{name}",)).fetchone()
    if not rec:
        return err("Receipt not found.", 404)
    if token:
        allowed = db.execute("SELECT 1 FROM group_member WHERE guest_token=? AND group_id=? AND active=1",
                             (token, rec["group_id"])).fetchone()
    else:
        allowed = member_row(rec["group_id"])
    if not allowed:
        return err("You do not have access to this receipt.", 403)
    if rec["content"] is not None:
        return Response(bytes(rec["content"]), mimetype=rec["content_type"] or "application/octet-stream",
                        headers={"Cache-Control": "private, max-age=3600"})
    return send_from_directory(current_app.config["UPLOAD_DIR"], name)
