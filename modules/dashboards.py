from flask import Blueprint, jsonify, request, session

from .common import group_or_error, login_required, member_name, money
from .currency import CODES, get_rate
from .db import get_db
from .settlements import balances_payload, compute_balances

bp = Blueprint("dashboards", __name__)


def group_summary(gid):
    db = get_db()
    total = db.execute("SELECT COALESCE(SUM(amount_base),0) t FROM expense WHERE group_id=?", (gid,)).fetchone()["t"]
    by_cat = db.execute("""SELECT category, SUM(amount_base) total, COUNT(*) count FROM expense
                           WHERE group_id=? GROUP BY category ORDER BY total DESC""", (gid,)).fetchall()
    by_cur = db.execute("""SELECT currency, SUM(amount) original_total, SUM(amount_base) base_total, COUNT(*) count
                           FROM expense WHERE group_id=? GROUP BY currency ORDER BY base_total DESC""", (gid,)).fetchall()
    by_day = db.execute("""SELECT expense_date AS day, SUM(amount_base) total FROM expense
                           WHERE group_id=? GROUP BY expense_date ORDER BY expense_date DESC LIMIT 10""", (gid,)).fetchall()
    rounded = lambda rows: [{k: money(v) if k in ("total", "original_total", "base_total") else v for k, v in dict(r).items()}
                            for r in rows]
    members, net, paid, owed = compute_balances(gid)
    by_participant = [{"member_id": m["member_id"], "name": member_name(m), "paid": money(paid[m["member_id"]]),
                       "spent": money(owed[m["member_id"]]), "net": net[m["member_id"]]}
                      for m in members if m["active"] or paid[m["member_id"]] or owed[m["member_id"]]]
    return {"total_spent": money(total), "by_category": rounded(by_cat), "by_currency": rounded(by_cur),
            "by_participant": by_participant, "by_day": rounded(reversed(by_day))}


@bp.get("/api/groups/<int:gid>/dashboard")
@login_required
def group_dashboard(gid):
    grp, e = group_or_error(gid)
    if e:
        return e
    out = group_summary(gid)
    out.update(balances_payload(gid))
    out["base_currency"] = grp["base_currency"]
    out["expense_count"] = get_db().execute("SELECT COUNT(*) c FROM expense WHERE group_id=?", (gid,)).fetchone()["c"]
    return jsonify(out)


@bp.get("/api/me/dashboard")
@login_required
def personal_dashboard():
    db = get_db()
    rows = db.execute("""SELECT g.group_id, g.group_name, g.base_currency, g.status, m.member_id FROM grp g
                         JOIN group_member m ON m.group_id=g.group_id WHERE m.user_id=? AND m.active=1
                         ORDER BY g.group_id DESC""", (session["uid"],)).fetchall()
    groups = []
    for r in rows:
        _, net, paid, owed = compute_balances(r["group_id"])
        groups.append({"group_id": r["group_id"], "group_name": r["group_name"], "base_currency": r["base_currency"],
                       "status": r["status"], "paid": money(paid[r["member_id"]]), "share": money(owed[r["member_id"]]),
                       "net": net[r["member_id"]]})
    cur = request.args.get("currency")
    if cur not in CODES:
        cur = groups[0]["base_currency"] if groups else "USD"
    totals, skipped, rates = {"currency": cur, "paid": 0.0, "share": 0.0, "net": 0.0}, [], {}
    for gr in groups:
        try:
            rates[gr["base_currency"]] = rate = get_rate(gr["base_currency"], cur)[0]
        except ValueError:
            skipped.append(gr["group_name"])
            gr["share_converted"] = None
            continue
        gr["share_converted"] = money(gr["share"] * rate)
        for k in ("paid", "share", "net"):
            totals[k] += gr[k] * rate
    totals = {k: (money(v) if k != "currency" else v) for k, v in totals.items()}

    by_cat = {}
    for r in db.execute("""SELECT e.category, g.base_currency, SUM(s.share_amount_base) amt FROM expense_split s
                           JOIN expense e ON e.expense_id=s.expense_id JOIN grp g ON g.group_id=e.group_id
                           JOIN group_member m ON m.member_id=s.member_id
                           WHERE m.user_id=? AND m.active=1 GROUP BY e.category, g.base_currency""", (session["uid"],)):
        if r["base_currency"] in rates:
            by_cat[r["category"]] = by_cat.get(r["category"], 0) + r["amt"] * rates[r["base_currency"]]
    by_category = sorted(({"category": k, "total": money(v)} for k, v in by_cat.items() if v > 0), key=lambda x: -x["total"])

    recent = []
    for r in db.execute("""SELECT e.expense_id, e.description, e.category, e.amount, e.currency, e.expense_date, e.paid_by,
                                  g.group_id, g.group_name FROM expense e JOIN grp g ON g.group_id=e.group_id
                           JOIN group_member m ON m.group_id=g.group_id AND m.user_id=? AND m.active=1
                           ORDER BY e.expense_date DESC, e.expense_id DESC LIMIT 6""", (session["uid"],)).fetchall():
        payer = db.execute("SELECT * FROM group_member WHERE member_id=?", (r["paid_by"],)).fetchone()
        recent.append(dict(r, paid_by_name=member_name(payer)))
    return jsonify({"groups": groups, "totals": totals, "not_converted": skipped, "by_category": by_category, "recent": recent})
