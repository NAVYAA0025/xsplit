from collections import defaultdict

from flask import Blueprint, jsonify

from .common import body, err, group_or_error, login_required, member_name, money, positive_number
from .db import get_db

bp = Blueprint("settlements", __name__)

EXACT_LIMIT = 14


def compute_balances(gid):
    db = get_db()
    members = db.execute("SELECT * FROM group_member WHERE group_id=?", (gid,)).fetchall()
    paid, owed, settled = defaultdict(float), defaultdict(float), defaultdict(float)
    for e in db.execute("SELECT paid_by, amount_base FROM expense WHERE group_id=?", (gid,)):
        paid[e["paid_by"]] += e["amount_base"]
    for s in db.execute("""SELECT s.member_id, s.share_amount_base FROM expense_split s
                           JOIN expense e ON e.expense_id=s.expense_id WHERE e.group_id=?""", (gid,)):
        owed[s["member_id"]] += s["share_amount_base"]
    for st in db.execute("SELECT payer_id, receiver_id, amount_base FROM settlement WHERE group_id=? AND status='Completed'", (gid,)):
        settled[st["payer_id"]] += st["amount_base"]
        settled[st["receiver_id"]] -= st["amount_base"]
    net = {m["member_id"]: money(paid[m["member_id"]] - owed[m["member_id"]] + settled[m["member_id"]]) for m in members}
    return members, net, paid, owed


def _greedy(cents):
    out = []
    creditors = sorted([[k, v] for k, v in cents.items() if v > 0], key=lambda x: -x[1])
    debtors = sorted([[k, -v] for k, v in cents.items() if v < 0], key=lambda x: -x[1])
    while creditors and debtors:
        c, d = creditors[0], debtors[0]
        amt = min(c[1], d[1])
        out.append({"from": d[0], "to": c[0], "amount": amt / 100})
        c[1] -= amt
        d[1] -= amt
        if c[1] == 0:
            creditors.pop(0)
        if d[1] == 0:
            debtors.pop(0)
        creditors.sort(key=lambda x: -x[1])
        debtors.sort(key=lambda x: -x[1])
    return out


def simplify_debts(net):
    cents = {k: round(v * 100) for k, v in net.items() if round(v * 100) != 0}
    keys = list(cents)
    n = len(keys)
    if n > EXACT_LIMIT or sum(cents.values()) != 0:
        return _greedy(cents)
    vals = [cents[k] for k in keys]
    total = [0] * (1 << n)
    for mask in range(1, 1 << n):
        low = mask & -mask
        total[mask] = total[mask ^ low] + vals[low.bit_length() - 1]
    memo = {0: []}

    def best(mask):
        if mask in memo:
            return memo[mask]
        low, result, sub = mask & -mask, None, mask
        while sub:
            if sub & low and total[sub] == 0:
                cand = [sub] + best(mask ^ sub)
                if result is None or len(cand) > len(result):
                    result = cand
            sub = (sub - 1) & mask
        memo[mask] = result
        return result

    out = []
    for grp in best((1 << n) - 1):
        out += _greedy({keys[i]: vals[i] for i in range(n) if grp >> i & 1})
    return out


def balances_payload(gid):
    members, net, paid, owed = compute_balances(gid)
    names = {m["member_id"]: member_name(m) for m in members}
    shown = [m for m in members if m["active"] or abs(net[m["member_id"]]) > 0.009]
    return {
        "balances": [{"member_id": m["member_id"], "name": names[m["member_id"]], "paid": money(paid[m["member_id"]]),
                      "share": money(owed[m["member_id"]]), "net": net[m["member_id"]]} for m in shown],
        "suggested_settlements": [dict(s, from_name=names[s["from"]], to_name=names[s["to"]])
                                  for s in simplify_debts(net)],
    }


def settlement_history(gid):
    db = get_db()
    names = {m["member_id"]: member_name(m) for m in db.execute("SELECT * FROM group_member WHERE group_id=?", (gid,))}
    rows = db.execute("SELECT * FROM settlement WHERE group_id=? ORDER BY settlement_id DESC", (gid,)).fetchall()
    return [dict(r, payer_name=names[r["payer_id"]], receiver_name=names[r["receiver_id"]]) for r in rows]


@bp.get("/api/groups/<int:gid>/balances")
@login_required
def balances(gid):
    grp, e = group_or_error(gid)
    if e:
        return e
    p = balances_payload(gid)
    p["base_currency"] = grp["base_currency"]
    return jsonify(p)


@bp.post("/api/groups/<int:gid>/settlements")
@login_required
def add_settlement(gid):
    grp, e = group_or_error(gid)
    if e:
        return e
    if grp["status"] == "Completed":
        return err("This group is closed.")
    d = body()
    try:
        amt = money(positive_number(d.get("amount")))
    except ValueError as ex:
        return err(str(ex))
    if d.get("payer_id") == d.get("receiver_id"):
        return err("Payer and receiver must be different.")
    db = get_db()
    ids = {r["member_id"] for r in db.execute("SELECT member_id FROM group_member WHERE group_id=? AND active=1", (gid,))}
    if d.get("payer_id") not in ids or d.get("receiver_id") not in ids:
        return err("Both people must belong to this group.")
    _, net, _, _ = compute_balances(gid)
    limit = min(-net[d["payer_id"]], net[d["receiver_id"]])
    if amt > limit + 0.009:
        return err(f"That is more than is owed. The most this payer can pay this receiver is "
                   f"{max(limit, 0):.2f} {grp['base_currency']}.")
    notes = (d.get("notes") or "").strip()[:200] or None
    db.execute("INSERT INTO settlement(group_id,payer_id,receiver_id,amount_base,notes) VALUES(?,?,?,?,?)",
               (gid, d["payer_id"], d["receiver_id"], amt, notes))
    db.commit()
    return jsonify(balances_payload(gid)), 201


@bp.get("/api/groups/<int:gid>/settlements")
@login_required
def list_settlements(gid):
    grp, e = group_or_error(gid)
    if e:
        return e
    return jsonify(settlement_history(gid))
