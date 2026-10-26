import secrets

from flask import Blueprint, jsonify, session

from .common import body, err, group_or_error, is_admin, login_required, member_name, member_row, money
from .currency import CODES, get_rate
from .db import get_db
from .settlements import compute_balances
from .splits import allocate

bp = Blueprint("groups", __name__)


@bp.post("/api/groups")
@login_required
def create_group():
    d = body()
    name, cur = (d.get("group_name") or "").strip(), d.get("base_currency")
    if not name or len(name) > 80:
        return err("Group name is required.")
    if cur not in CODES:
        return err("Choose a supported base currency.")
    db = get_db()
    c = db.execute("INSERT INTO grp(group_name, base_currency, created_by) VALUES(?,?,?)", (name, cur, session["uid"]))
    db.execute("INSERT INTO group_member(group_id,user_id,role) VALUES(?,?, 'Admin')", (c.lastrowid, session["uid"]))
    db.commit()
    return jsonify({"group_id": c.lastrowid}), 201


@bp.get("/api/groups")
@login_required
def list_groups():
    rows = get_db().execute("""SELECT g.* FROM grp g JOIN group_member m ON m.group_id=g.group_id
                               WHERE m.user_id=? AND m.active=1 ORDER BY g.group_id DESC""", (session["uid"],)).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.get("/api/groups/<int:gid>")
@login_required
def get_group(gid):
    grp, e = group_or_error(gid)
    if e:
        return e
    db = get_db()
    out = dict(grp)
    out["is_admin"] = is_admin(gid)
    out["my_member_id"] = member_row(gid)["member_id"]
    out["members"] = []
    for m in db.execute("SELECT * FROM group_member WHERE group_id=? AND active=1", (gid,)).fetchall():
        item = {"member_id": m["member_id"], "name": member_name(m), "role": m["role"], "is_guest": bool(m["is_guest"])}
        if m["is_guest"] and out["is_admin"]:
            item["guest_link"] = f"/guest/{m['guest_token']}"
        out["members"].append(item)
    out["pending_invites"] = [{"member_id": m["member_id"], "name": member_name(m)} for m in
                              db.execute("SELECT * FROM group_member WHERE group_id=? AND invite_status='Pending'", (gid,))]
    out["has_expenses"] = bool(db.execute("SELECT 1 FROM expense WHERE group_id=?", (gid,)).fetchone())
    return jsonify(out)


def rebase_group(db, gid, factor):
    for e in db.execute("SELECT expense_id, amount, exchange_rate_used FROM expense WHERE group_id=?", (gid,)).fetchall():
        rate = e["exchange_rate_used"] * factor
        total_cents = round(e["amount"] * rate * 100)
        splits = db.execute("SELECT split_id, share_amount_original FROM expense_split WHERE expense_id=? ORDER BY split_id",
                            (e["expense_id"],)).fetchall()
        shares = allocate(total_cents, [s["share_amount_original"] for s in splits]) if splits else []
        db.execute("UPDATE expense SET exchange_rate_used=?, amount_base=?, rate_source='rebased' WHERE expense_id=?",
                   (rate, total_cents / 100, e["expense_id"]))
        for s, cents in zip(splits, shares):
            db.execute("UPDATE expense_split SET share_amount_base=? WHERE split_id=?", (cents / 100, s["split_id"]))
    for s in db.execute("SELECT settlement_id, amount_base FROM settlement WHERE group_id=?", (gid,)).fetchall():
        db.execute("UPDATE settlement SET amount_base=? WHERE settlement_id=?", (money(s["amount_base"] * factor), s["settlement_id"]))


@bp.put("/api/groups/<int:gid>")
@login_required
def update_group(gid):
    grp, e = group_or_error(gid)
    if e:
        return e
    if not is_admin(gid):
        return err("Only the group creator can edit group details.", 403)
    d = body()
    name = (d.get("group_name") or grp["group_name"]).strip()
    cur = d.get("base_currency") or grp["base_currency"]
    if not name or len(name) > 80:
        return err("Group name is required.")
    if cur not in CODES:
        return err("Choose a supported base currency.")
    db = get_db()
    db.execute("BEGIN")
    if cur != grp["base_currency"]:
        if grp["status"] == "Completed":
            db.rollback()
            return err("The base currency of a closed group cannot be changed.")
        try:
            factor, _ = get_rate(grp["base_currency"], cur)
        except ValueError:
            db.rollback()
            return err("Exchange rate service unavailable, so the currency was not changed. Try again later.", 503)
        rebase_group(db, gid, factor)
    db.execute("UPDATE grp SET group_name=?, base_currency=? WHERE group_id=?", (name, cur, gid))
    db.commit()
    return get_group(gid)


@bp.post("/api/groups/<int:gid>/members")
@login_required
def add_member(gid):
    grp, e = group_or_error(gid)
    if e:
        return e
    if grp["status"] == "Completed":
        return err("This group is closed.")
    d = body()
    db = get_db()
    if "guest_name" in d:
        if not is_admin(gid):
            return err("Only the group creator can add guests.", 403)
        name = (d.get("guest_name") or "").strip()
        if not name or len(name) > 80:
            return err("Enter the guest's name.")
        db.execute("INSERT INTO group_member(group_id,guest_name,guest_contact,guest_token,is_guest) VALUES(?,?,?,?,1)",
                   (gid, name, (d.get("guest_contact") or "").strip() or None, secrets.token_urlsafe(16)))
        db.commit()
        return get_group(gid)
    u = db.execute("SELECT user_id FROM user WHERE email=?", ((d.get("email") or "").strip().lower(),)).fetchone()
    if not u:
        return err("No X-Split user with that email. Add them as a guest instead.", 404)
    ex = db.execute("SELECT * FROM group_member WHERE group_id=? AND user_id=?", (gid, u["user_id"])).fetchone()
    if ex and ex["active"]:
        return err("That user is already in the group.", 409)
    if ex and ex["invite_status"] == "Pending":
        return err("That user has already been invited.", 409)
    if ex:
        db.execute("UPDATE group_member SET invite_status='Pending', invited_by=? WHERE member_id=?", (session["uid"], ex["member_id"]))
    else:
        db.execute("INSERT INTO group_member(group_id,user_id,active,invite_status,invited_by) VALUES(?,?,0,'Pending',?)",
                   (gid, u["user_id"], session["uid"]))
    db.commit()
    return get_group(gid)


@bp.delete("/api/groups/<int:gid>/members/<int:mid>")
@login_required
def remove_member(gid, mid):
    grp, e = group_or_error(gid)
    if e:
        return e
    if not is_admin(gid):
        return err("Only the group creator can remove members.", 403)
    m = get_db().execute("SELECT * FROM group_member WHERE member_id=? AND group_id=?", (mid, gid)).fetchone()
    if not m:
        return err("Member not found.", 404)
    if m["role"] == "Admin":
        return err("The group creator cannot be removed.")
    _, net, _, _ = compute_balances(gid)
    if abs(net.get(mid, 0)) > 0.009:
        return err("Member has an unsettled balance. Settle up before removing.")
    get_db().execute("UPDATE group_member SET active=0, invite_status=NULL WHERE member_id=?", (mid,))
    get_db().commit()
    return get_group(gid)


@bp.post("/api/groups/<int:gid>/close")
@login_required
def close_group(gid):
    grp, e = group_or_error(gid)
    if e:
        return e
    if not is_admin(gid):
        return err("Only the group creator can close the group.", 403)
    _, net, _, _ = compute_balances(gid)
    if any(abs(v) > 0.009 for v in net.values()):
        return err("Cannot close: outstanding balances remain.")
    get_db().execute("UPDATE grp SET status='Completed' WHERE group_id=?", (gid,))
    get_db().commit()
    return get_group(gid)


@bp.get("/api/invitations")
@login_required
def list_invitations():
    rows = get_db().execute("""SELECT m.member_id, g.group_id, g.group_name, g.base_currency, u.full_name AS invited_by_name
                               FROM group_member m JOIN grp g ON g.group_id=m.group_id LEFT JOIN user u ON u.user_id=m.invited_by
                               WHERE m.user_id=? AND m.invite_status='Pending' ORDER BY m.member_id DESC""",
                            (session["uid"],)).fetchall()
    return jsonify([dict(r) for r in rows])


def _my_invitation(mid):
    return get_db().execute("""SELECT m.*, g.status FROM group_member m JOIN grp g ON g.group_id=m.group_id
                               WHERE m.member_id=? AND m.user_id=? AND m.invite_status='Pending'""",
                            (mid, session["uid"])).fetchone()


@bp.post("/api/invitations/<int:mid>/accept")
@login_required
def accept_invitation(mid):
    inv = _my_invitation(mid)
    if not inv:
        return err("Invitation not found.", 404)
    if inv["status"] == "Completed":
        return err("This group has been closed.")
    get_db().execute("UPDATE group_member SET active=1, invite_status='Accepted', join_date=CURRENT_TIMESTAMP WHERE member_id=?", (mid,))
    get_db().commit()
    return jsonify({"group_id": inv["group_id"]})


@bp.post("/api/invitations/<int:mid>/decline")
@login_required
def decline_invitation(mid):
    if not _my_invitation(mid):
        return err("Invitation not found.", 404)
    get_db().execute("UPDATE group_member SET invite_status='Declined' WHERE member_id=?", (mid,))
    get_db().commit()
    return list_invitations()
