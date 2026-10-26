import re

from flask import Blueprint, jsonify, session
from werkzeug.security import check_password_hash, generate_password_hash

from .common import body, err, login_required
from .db import get_db

bp = Blueprint("users", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_phone(value):
    phone = (value or "").strip()
    if phone and not re.fullmatch(r"[0-9+()\-\s]{3,20}", phone):
        raise ValueError("Enter a valid phone number.")
    return phone or None


@bp.post("/api/register")
def register():
    d = body()
    name, email, pw = (d.get("full_name") or "").strip(), (d.get("email") or "").strip().lower(), d.get("password") or ""
    if not name or len(name) > 80 or not EMAIL_RE.match(email):
        return err("Enter a valid name and email.")
    if len(pw) < 6:
        return err("Password must be at least 6 characters.")
    try:
        phone = _clean_phone(d.get("phone"))
    except ValueError as ex:
        return err(str(ex))
    db = get_db()
    if db.execute("SELECT 1 FROM user WHERE email=?", (email,)).fetchone():
        return err("An account with this email already exists.", 409)
    cur = db.execute("INSERT INTO user(full_name,email,password_hash,phone) VALUES(?,?,?,?)",
                     (name, email, generate_password_hash(pw), phone))
    db.commit()
    session.clear()
    session["uid"] = cur.lastrowid
    return jsonify({"user_id": cur.lastrowid, "full_name": name, "email": email}), 201


@bp.post("/api/login")
def login():
    d = body()
    u = get_db().execute("SELECT * FROM user WHERE email=?", ((d.get("email") or "").strip().lower(),)).fetchone()
    if not u or not check_password_hash(u["password_hash"], d.get("password") or ""):
        return err("Invalid email or password.", 401)
    session.clear()
    session["uid"] = u["user_id"]
    return jsonify({"user_id": u["user_id"], "full_name": u["full_name"], "email": u["email"]})


@bp.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@bp.get("/api/me")
@login_required
def me():
    u = get_db().execute("SELECT user_id, full_name, email, phone FROM user WHERE user_id=?", (session["uid"],)).fetchone()
    if not u:
        session.clear()
        return err("Please log in.", 401)
    return jsonify(dict(u))


@bp.put("/api/me")
@login_required
def update_me():
    d = body()
    db = get_db()
    name = (d.get("full_name") or "").strip()
    if not name or len(name) > 80:
        return err("Name cannot be empty.")
    try:
        phone = _clean_phone(d.get("phone"))
    except ValueError as ex:
        return err(str(ex))
    if d.get("new_password"):
        u = db.execute("SELECT password_hash FROM user WHERE user_id=?", (session["uid"],)).fetchone()
        if not check_password_hash(u["password_hash"], d.get("current_password") or ""):
            return err("Your current password is incorrect.", 403)
        if len(d["new_password"]) < 6:
            return err("New password must be at least 6 characters.")
        db.execute("UPDATE user SET password_hash=? WHERE user_id=?", (generate_password_hash(d["new_password"]), session["uid"]))
    db.execute("UPDATE user SET full_name=?, phone=? WHERE user_id=?", (name, phone, session["uid"]))
    db.commit()
    return me()


@bp.get("/api/friends")
@login_required
def list_friends():
    rows = get_db().execute("""SELECT u.user_id, u.full_name, u.email FROM friend f JOIN user u ON u.user_id=f.friend_id
                               WHERE f.user_id=? ORDER BY u.full_name""", (session["uid"],)).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.post("/api/friends")
@login_required
def add_friend():
    d = body()
    db = get_db()
    u = db.execute("SELECT user_id FROM user WHERE email=?", ((d.get("email") or "").strip().lower(),)).fetchone()
    if not u:
        return err("No X-Split user with that email.", 404)
    if u["user_id"] == session["uid"]:
        return err("You cannot add yourself.")
    db.execute("INSERT OR IGNORE INTO friend VALUES(?,?)", (session["uid"], u["user_id"]))
    db.execute("INSERT OR IGNORE INTO friend VALUES(?,?)", (u["user_id"], session["uid"]))
    db.commit()
    return list_friends()


@bp.delete("/api/friends/<int:fid>")
@login_required
def remove_friend(fid):
    db = get_db()
    db.execute("DELETE FROM friend WHERE (user_id=? AND friend_id=?) OR (user_id=? AND friend_id=?)",
               (session["uid"], fid, fid, session["uid"]))
    db.commit()
    return list_friends()
