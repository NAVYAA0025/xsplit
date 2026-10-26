from flask import Blueprint, jsonify

from .common import err
from .db import get_db
from .expenses import expense_dict
from .settlements import balances_payload, settlement_history

bp = Blueprint("guest", __name__)


@bp.get("/api/guest/<token>")
def guest_view(token):
    db = get_db()
    m = db.execute("SELECT * FROM group_member WHERE guest_token=? AND active=1", (token,)).fetchone()
    if not m:
        return err("Invalid or expired guest link.", 404)
    grp = db.execute("SELECT * FROM grp WHERE group_id=?", (m["group_id"],)).fetchone()
    exps = db.execute("SELECT * FROM expense WHERE group_id=? ORDER BY expense_date DESC, expense_id DESC", (grp["group_id"],)).fetchall()
    expenses = []
    for x in exps:
        d = expense_dict(x)
        if d["receipt_url"]:
            d["receipt_url"] += f"?guest={token}"
        expenses.append(d)
    return jsonify({"group_name": grp["group_name"], "base_currency": grp["base_currency"], "status": grp["status"],
                    "you": m["guest_name"], "my_member_id": m["member_id"], "expenses": expenses,
                    "settlements": settlement_history(grp["group_id"]), **balances_payload(grp["group_id"])})
