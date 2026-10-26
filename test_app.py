import io
import json
import os
import pytest
from app import create_app


def fresh_postgres():
    url = os.environ.get("XSPLIT_TEST_DATABASE_URL")
    if url:
        import psycopg
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    return url


def close_pool(app):
    pool = app.extensions.get("xsplit_pg")
    if pool:
        pool.close()


@pytest.fixture
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "t.db"), testing=True, database_url=fresh_postgres())
    app.config["UPLOAD_DIR"] = str(tmp_path)
    yield app.test_client()
    close_pool(app)


def reg(c, name, email):
    return c.post("/api/register", json={"full_name": name, "email": email, "password": "secret1"})


def setup_group(c, base="EUR"):
    reg(c, "Navyaa", "navyaa@example.com")
    gid = c.post("/api/groups", json={"group_name": "Europe Trip", "base_currency": base}).get_json()["group_id"]
    c.post(f"/api/groups/{gid}/members", json={"guest_name": "Arjun"})
    c.post(f"/api/groups/{gid}/members", json={"guest_name": "Chandana"})
    g = c.get(f"/api/groups/{gid}").get_json()
    ids = {m["name"].split(" (")[0]: m["member_id"] for m in g["members"]}
    return gid, ids


def add(c, gid, ids, amount, cur, payer, split="equal", parts=None, **kw):
    parts = parts or [{"member_id": i, "value": 1} for i in ids.values()]
    body = dict(description="x", category="Food", amount=amount, currency=cur, paid_by=ids[payer],
                split_type=split, participants=parts, expense_date="2026-10-10")
    body.update(kw)
    return c.post(f"/api/groups/{gid}/expenses", json=body)


def test_ft01_register_and_ft02_login(client):
    assert reg(client, "Navyaa", "navyaa@example.com").status_code == 201
    client.post("/api/logout")
    assert client.post("/api/login", json={"email": "navyaa@example.com", "password": "bad"}).status_code == 401
    assert client.post("/api/login", json={"email": "navyaa@example.com", "password": "secret1"}).status_code == 200


def test_ft05_group_and_ft08_guest(client):
    gid, ids = setup_group(client)
    assert set(ids) == {"Navyaa", "Arjun", "Chandana"}


def test_ft13_14_conversion_and_rate_stored(client):
    gid, ids = setup_group(client)
    r = add(client, gid, ids, 100, "USD", "Navyaa", manual_rate=0.92).get_json()
    assert r["amount_base"] == 92.0 and r["exchange_rate_used"] == 0.92


def test_ft15_equal_split_no_lost_cents(client):
    gid, ids = setup_group(client)
    r = add(client, gid, ids, 100, "EUR", "Navyaa").get_json()
    shares = [s["share_base"] for s in r["splits"]]
    assert round(sum(shares), 2) == 100.0 and sorted(shares) == [33.33, 33.33, 33.34]


def test_ft16_unequal_mismatch_rejected(client):
    gid, ids = setup_group(client)
    parts = [{"member_id": i, "value": v} for i, v in zip(ids.values(), [40, 30, 20])]
    r = add(client, gid, ids, 100, "EUR", "Navyaa", "unequal", parts)
    assert r.status_code == 400 and "total" in r.get_json()["error"].lower()


def test_ft17_percentage_split(client):
    gid, ids = setup_group(client)
    parts = [{"member_id": i, "value": v} for i, v in zip(ids.values(), [50, 30, 20])]
    r = add(client, gid, ids, 200, "EUR", "Navyaa", "percent", parts).get_json()
    assert [s["share_base"] for s in r["splits"]] == [100.0, 60.0, 40.0]


def test_ft18_selective_participation(client):
    gid, ids = setup_group(client)
    parts = [{"member_id": ids["Navyaa"], "value": 1}, {"member_id": ids["Arjun"], "value": 1}]
    r = add(client, gid, ids, 80, "EUR", "Navyaa", "equal", parts).get_json()
    assert [s["share_base"] for s in r["splits"]] == [40.0, 40.0]


def test_ft25_balances(client):
    gid, ids = setup_group(client)
    add(client, gid, ids, 90, "EUR", "Navyaa")
    b = {x["name"].split(" (")[0]: x["net"] for x in client.get(f"/api/groups/{gid}/balances").get_json()["balances"]}
    assert b == {"Navyaa": 60.0, "Arjun": -30.0, "Chandana": -30.0}


def test_ft26_debt_simplification(client):
    gid, ids = setup_group(client)

    add(client, gid, ids, 20, "EUR", "Navyaa", "equal", [{"member_id": ids["Arjun"], "value": 1}])
    add(client, gid, ids, 20, "EUR", "Chandana", "equal", [{"member_id": ids["Navyaa"], "value": 1}])
    s = client.get(f"/api/groups/{gid}/balances").get_json()["suggested_settlements"]
    assert len(s) == 1 and s[0]["amount"] == 20.0
    assert s[0]["from"] == ids["Arjun"] and s[0]["to"] == ids["Chandana"]


def test_ft27_28_settlement_and_outstanding(client):
    gid, ids = setup_group(client)
    add(client, gid, ids, 90, "EUR", "Navyaa")
    client.post(f"/api/groups/{gid}/settlements", json={"payer_id": ids["Arjun"], "receiver_id": ids["Navyaa"], "amount": 25})
    b = {x["name"].split(" (")[0]: x["net"] for x in client.get(f"/api/groups/{gid}/balances").get_json()["balances"]}
    assert b["Arjun"] == -5.0


def test_ft21_22_edit_and_delete(client):
    gid, ids = setup_group(client)
    e = add(client, gid, ids, 60, "EUR", "Navyaa").get_json()
    body = dict(description="x", category="Food", amount=65, currency="EUR", paid_by=ids["Navyaa"], split_type="equal",
                participants=[{"member_id": i, "value": 1} for i in ids.values()])
    assert client.put(f"/api/expenses/{e['expense_id']}", json=body).get_json()["amount"] == 65
    assert client.delete(f"/api/expenses/{e['expense_id']}").status_code == 200
    assert client.get(f"/api/groups/{gid}/expenses").get_json() == []


def test_ft32_close_group_requires_settled(client):
    gid, ids = setup_group(client)
    add(client, gid, ids, 90, "EUR", "Navyaa")
    assert client.post(f"/api/groups/{gid}/close").status_code == 400
    for n in ("Arjun", "Chandana"):
        client.post(f"/api/groups/{gid}/settlements", json={"payer_id": ids[n], "receiver_id": ids["Navyaa"], "amount": 30})
    assert client.post(f"/api/groups/{gid}/close").get_json()["status"] == "Completed"


def test_nft03_unauthorized_group_access(client):
    gid, _ = setup_group(client)
    client.post("/api/logout")
    reg(client, "Other", "other@example.com")
    assert client.get(f"/api/groups/{gid}").status_code == 403
    assert client.get(f"/api/groups/{gid}/expenses").status_code == 403


def test_nft05_integrity_shares_sum_to_total(client):
    gid, ids = setup_group(client)
    r = add(client, gid, ids, 120, "USD", "Navyaa", manual_rate=0.9137).get_json()
    assert round(sum(s["share_base"] for s in r["splits"]), 2) == r["amount_base"]


def test_nft11_invalid_input_no_partial_save(client):
    gid, ids = setup_group(client)
    assert add(client, gid, ids, -5, "EUR", "Navyaa").status_code == 400
    assert client.get(f"/api/groups/{gid}/expenses").get_json() == []


def test_guest_link_read_only_view(client):
    gid, ids = setup_group(client)
    add(client, gid, ids, 90, "EUR", "Navyaa")
    link = [m for m in client.get(f"/api/groups/{gid}").get_json()["members"] if m["is_guest"]][0]["guest_link"]
    c2 = client.application.test_client()
    d = c2.get("/api/" + link.lstrip("/")).get_json()
    assert d["group_name"] == "Europe Trip" and len(d["expenses"]) == 1
    assert c2.get("/api/guest/bogus").status_code == 404


from app import parse_receipt_text


@pytest.mark.parametrize("text,amount,cur", [
    ("Cafe\nSubtotal 40.00\nTAX 5.50\nTOTAL EUR 45.50", 45.5, "EUR"),
    ("Total: 1.234,50 €", 1234.5, "EUR"),
    ("Grand Total $1,234.50", 1234.5, "USD"),
    ("Amount due 45,50 EUR", 45.5, "EUR"),
    ("Coffee 3.20\nCake 4.80", 4.8, None),
])
def test_ft20_receipt_parser(text, amount, cur):
    r = parse_receipt_text(text)
    assert r["amount"] == amount and r["currency"] == cur


def test_ft03_ft04_profile_and_friends(client):
    reg(client, "Navyaa", "navyaa@example.com")
    assert client.put("/api/me", json={"full_name": "Navyaa S", "phone": "999"}).get_json()["full_name"] == "Navyaa S"
    assert client.put("/api/me", json={"full_name": " "}).status_code == 400
    client.post("/api/logout"); reg(client, "Tamanna", "tamanna@example.com")
    client.post("/api/logout"); client.post("/api/login", json={"email": "navyaa@example.com", "password": "secret1"})
    assert client.post("/api/friends", json={"email": "nobody@example.com"}).status_code == 404
    assert client.post("/api/friends", json={"email": "navyaa@example.com"}).status_code == 400
    assert len(client.post("/api/friends", json={"email": "tamanna@example.com"}).get_json()) == 1
    assert client.delete("/api/friends/2").get_json() == []


def login(c, email):
    c.post("/api/logout")
    return c.post("/api/login", json={"email": email, "password": "secret1"})


def invite_tamanna(c, gid, accept=True):
    c.post("/api/logout"); reg(c, "Tamanna", "tamanna@example.com")
    login(c, "navyaa@example.com")
    c.post(f"/api/groups/{gid}/members", json={"email": "tamanna@example.com"})
    login(c, "tamanna@example.com")
    inv = c.get("/api/invitations").get_json()[0]
    if accept:
        c.post(f"/api/invitations/{inv['member_id']}/accept")
    return inv["member_id"]


def test_ft07_invitation_must_be_accepted(client):
    gid, ids = setup_group(client)
    client.post("/api/logout"); reg(client, "Tamanna", "tamanna@example.com")
    login(client, "navyaa@example.com")
    r = client.post(f"/api/groups/{gid}/members", json={"email": "tamanna@example.com"})
    assert r.status_code == 200 and [p["name"] for p in r.get_json()["pending_invites"]] == ["Tamanna"]
    assert client.post(f"/api/groups/{gid}/members", json={"email": "tamanna@example.com"}).status_code == 409
    login(client, "tamanna@example.com")
    assert client.get(f"/api/groups/{gid}").status_code == 403
    inv = client.get("/api/invitations").get_json()
    assert inv[0]["group_name"] == "Europe Trip" and inv[0]["invited_by_name"] == "Navyaa"
    assert client.post(f"/api/invitations/{inv[0]['member_id']}/accept").get_json()["group_id"] == gid
    assert "Tamanna" in [m["name"] for m in client.get(f"/api/groups/{gid}").get_json()["members"]]
    assert client.get("/api/invitations").get_json() == []

    client.post("/api/logout"); reg(client, "Chandana", "chandana@example.com")
    login(client, "tamanna@example.com")
    assert client.post(f"/api/groups/{gid}/members", json={"email": "chandana@example.com"}).status_code == 200
    assert client.post(f"/api/groups/{gid}/members", json={"guest_name": "X"}).status_code == 403
    assert client.put(f"/api/groups/{gid}", json={"group_name": "Hijack"}).status_code == 403
    login(client, "chandana@example.com")
    mid = client.get("/api/invitations").get_json()[0]["member_id"]
    assert client.post(f"/api/invitations/{mid}/decline").get_json() == []
    assert client.get(f"/api/groups/{gid}").status_code == 403


def test_ft09_change_base_currency_reconverts_everything(client):
    gid, ids = setup_group(client)
    assert client.put(f"/api/groups/{gid}", json={"group_name": "Europe Tour"}).get_json()["group_name"] == "Europe Tour"
    add(client, gid, ids, 90, "EUR", "Navyaa")
    add(client, gid, ids, 100, "USD", "Arjun", manual_rate=0.92)
    client.post(f"/api/groups/{gid}/settlements", json={"payer_id": ids["Chandana"], "receiver_id": ids["Navyaa"], "amount": 30})
    before = {b["member_id"]: b["net"] for b in client.get(f"/api/groups/{gid}/balances").get_json()["balances"]}
    g = client.put(f"/api/groups/{gid}", json={"base_currency": "USD"}).get_json()
    assert g["base_currency"] == "USD"
    exps = client.get(f"/api/groups/{gid}/expenses").get_json()
    usd = [e for e in exps if e["currency"] == "USD"][0]
    assert abs(usd["amount_base"] - 100.0) < 0.01
    for e in exps:
        assert abs(sum(s["share_base"] for s in e["splits"]) - e["amount_base"]) < 1e-9
    after = {b["member_id"]: b["net"] for b in client.get(f"/api/groups/{gid}/balances").get_json()["balances"]}
    for mid in before:
        assert abs(after[mid] - before[mid] / 0.92) < 0.02
    assert abs(sum(after.values())) < 0.011


def test_ft06_cannot_remove_member_with_unsettled_balance(client):
    gid, ids = setup_group(client)
    add(client, gid, ids, 90, "EUR", "Navyaa")
    assert client.delete(f"/api/groups/{gid}/members/{ids['Arjun']}").status_code == 400


def test_nft03_nft04_receipt_files_are_private(client):
    gid, ids = setup_group(client)
    body = dict(description="Dinner", category="Food", amount=45.5, currency="EUR", paid_by=ids["Navyaa"],
                split_type="equal", participants=[{"member_id": i, "value": 1} for i in ids.values()], expense_date="2026-10-10")
    r = client.post(f"/api/groups/{gid}/expenses", data={"data": json.dumps(body), "receipt": (io.BytesIO(b"\x89PNG fake"), "r.png")},
                    content_type="multipart/form-data")
    url = r.get_json()["receipt_url"]
    assert url and client.get(url).status_code == 200
    client.post("/api/logout"); reg(client, "Outsider", "out@example.com")
    assert client.get(url).status_code == 403
    client.post("/api/logout")
    assert client.get(url).status_code == 401
    assert client.post("/api/groups/1/expenses", json=body).status_code == 401


def test_edit_keeps_rate_source_when_rate_unchanged(client):
    gid, ids = setup_group(client)
    eid = add(client, gid, ids, 100, "USD", "Navyaa", manual_rate=0.92).get_json()["expense_id"]
    body = dict(description="x", category="Food", amount=200, currency="USD", paid_by=ids["Navyaa"], split_type="equal",
                participants=[{"member_id": i, "value": 1} for i in ids.values()], manual_rate=0.92, expense_date="2026-10-10")
    e = client.put(f"/api/expenses/{eid}", json=body).get_json()
    assert e["amount_base"] == 184.0 and e["exchange_rate_used"] == 0.92 and e["split_type"] == "equal"
    assert abs(sum(s["share_base"] for s in e["splits"]) - 184.0) < 1e-9


def test_ft29_ft30_ft31_dashboards(client):
    gid, ids = setup_group(client)
    add(client, gid, ids, 90, "EUR", "Navyaa")
    add(client, gid, ids, 100, "USD", "Arjun", manual_rate=0.92, category="Transport")
    d = client.get(f"/api/groups/{gid}/dashboard").get_json()
    assert d["total_spent"] == 182.0
    assert {c["category"]: c["total"] for c in d["by_category"]} == {"Food": 90.0, "Transport": 92.0}
    assert {c["currency"] for c in d["by_currency"]} == {"EUR", "USD"}
    me = client.get("/api/me/dashboard").get_json()["groups"][0]
    assert me["paid"] == 90.0 and abs(me["share"] - 60.67) < 0.01


def test_nft12_exchange_api_failure_does_not_corrupt_data(client):
    gid, ids = setup_group(client)
    assert client.get("/api/rate?from=XXX&to=EUR").status_code == 503
    bad = add(client, gid, ids, 10, "XXX", "Navyaa")
    assert bad.status_code == 400
    assert client.get(f"/api/groups/{gid}/expenses").get_json() == []


def test_settlement_reduces_balance_and_blocks_after_close(client):
    gid, ids = setup_group(client)
    add(client, gid, ids, 90, "EUR", "Navyaa")
    r = client.post(f"/api/groups/{gid}/settlements", json={"payer_id": ids["Arjun"], "receiver_id": ids["Navyaa"], "amount": 30})
    assert r.status_code == 201
    client.post(f"/api/groups/{gid}/settlements", json={"payer_id": ids["Chandana"], "receiver_id": ids["Navyaa"], "amount": 30})
    assert client.post(f"/api/groups/{gid}/close").status_code == 200
    assert add(client, gid, ids, 5, "EUR", "Navyaa").status_code == 400


def test_ft06_add_then_remove_settled_member(client):
    gid, ids = setup_group(client)
    client.post(f"/api/groups/{gid}/members", json={"guest_name": "Ravi"})
    rid = [m["member_id"] for m in client.get(f"/api/groups/{gid}").get_json()["members"] if m["name"].startswith("Ravi")][0]
    r = client.delete(f"/api/groups/{gid}/members/{rid}")
    assert r.status_code == 200 and all(m["member_id"] != rid for m in r.get_json()["members"])


def test_ft21_22_only_payer_or_admin_can_change_expense(client):
    gid, ids = setup_group(client)
    navyaas = add(client, gid, ids, 30, "EUR", "Navyaa").get_json()["expense_id"]
    tid = invite_tamanna(client, gid)
    assert client.delete(f"/api/expenses/{navyaas}").status_code == 403
    own = add(client, gid, dict(ids, Tamanna=tid), 12, "EUR", "Tamanna").get_json()["expense_id"]
    assert client.delete(f"/api/expenses/{own}").status_code == 200

def test_nft05_settlement_cannot_exceed_debt(client):
    gid, ids = setup_group(client)
    add(client, gid, ids, 90, "EUR", "Navyaa")
    r = client.post(f"/api/groups/{gid}/settlements", json={"payer_id": ids["Arjun"], "receiver_id": ids["Navyaa"], "amount": 31})
    assert r.status_code == 400 and "30.00" in r.get_json()["error"]
    b = {x["name"].split(" (")[0]: x["net"] for x in client.get(f"/api/groups/{gid}/balances").get_json()["balances"]}
    assert b["Arjun"] == -30.0


def test_guest_can_open_own_group_receipt_only(client):
    gid, ids = setup_group(client)
    body = dict(description="Dinner", category="Food", amount=30, currency="EUR", paid_by=ids["Navyaa"],
                split_type="equal", participants=[{"member_id": i, "value": 1} for i in ids.values()])
    client.post(f"/api/groups/{gid}/expenses", data={"data": json.dumps(body), "receipt": (io.BytesIO(b"\x89PNG fake"), "r.png")},
                content_type="multipart/form-data")
    link = [m for m in client.get(f"/api/groups/{gid}").get_json()["members"] if m["is_guest"]][0]["guest_link"]
    c2 = client.application.test_client()
    url = c2.get("/api/" + link.lstrip("/")).get_json()["expenses"][0]["receipt_url"]
    assert "?guest=" in url and c2.get(url).status_code == 200
    assert c2.get(url.split("?")[0] + "?guest=wrong").status_code == 403


from modules.settlements import simplify_debts


def _settles(net, payments):
    left = dict(net)
    for p in payments:
        left[p["from"]] += p["amount"]
        left[p["to"]] -= p["amount"]
    return all(abs(v) < 1e-9 for v in left.values())


@pytest.mark.parametrize("net,minimum", [
    ({"A": -9, "B": 7, "C": -2, "D": 5, "E": 6, "F": -7}, 4),
    ({"A": -20, "B": 0, "C": 20}, 1),
    ({"A": 10, "B": -10, "C": 5, "D": -5}, 2),
    ({"A": 0, "B": 0}, 0),
])
def test_fr26_simplification_is_minimal(net, minimum):
    s = simplify_debts(net)
    assert len(s) == minimum and _settles(net, s)


def test_fr26_large_group_still_settles():
    net = {i: (i % 7) - 3 for i in range(30)}
    net[0] -= sum(net.values())
    s = simplify_debts(net)
    assert _settles(net, s) and len(s) <= len([v for v in net.values() if v]) - 1


def test_fr19_receipt_replaced_on_edit_and_removed_on_delete(client):
    gid, ids = setup_group(client)
    body = dict(description="Dinner", category="Food", amount=30, currency="EUR", paid_by=ids["Navyaa"],
                split_type="equal", participants=[{"member_id": i, "value": 1} for i in ids.values()])
    form = lambda name, data: {"data": json.dumps(body), "receipt": (io.BytesIO(data), name)}
    e = client.post(f"/api/groups/{gid}/expenses", data=form("a.pdf", b"%PDF first"), content_type="multipart/form-data").get_json()
    first = e["receipt_url"]
    assert client.get(first).data == b"%PDF first"
    e2 = client.put(f"/api/expenses/{e['expense_id']}", data=form("b.pdf", b"%PDF second"), content_type="multipart/form-data").get_json()
    assert client.get(e2["receipt_url"]).data == b"%PDF second" and client.get(first).status_code == 404
    client.delete(f"/api/expenses/{e['expense_id']}")
    assert client.get(e2["receipt_url"]).status_code == 404


def test_fr19_receipt_photos_are_shrunk():
    from PIL import Image
    from modules.expenses import _shrink
    big = io.BytesIO(); Image.effect_noise((4000, 3000), 40).convert("RGB").save(big, "PNG")
    data, mime = _shrink(big.getvalue(), ".png")
    assert mime == "image/jpeg" and max(Image.open(io.BytesIO(data)).size) == 2000


@pytest.mark.parametrize("change,msg", [
    ({"amount": "nan"}, "amount"), ({"amount": "1e400"}, "amount"), ({"amount": "abc"}, "amount"),
    ({"description": "  "}, "description"), ({"expense_date": "2026-13-45"}, "date"),
    ({"manual_rate": "-1", "currency": "USD"}, "rate"), ({"participants": "everyone"}, "participant"),
])
def test_nfr11_invalid_expense_input_is_rejected_cleanly(client, change, msg):
    gid, ids = setup_group(client)
    change = dict(change)
    r = add(client, gid, ids, change.pop("amount", 10), change.pop("currency", "EUR"), "Navyaa", **change)
    assert r.status_code == 400 and msg in r.get_json()["error"].lower()
    assert client.get(f"/api/groups/{gid}/expenses").get_json() == []


def test_nfr11_errors_are_json(client):
    reg(client, "Navyaa", "navyaa@example.com")
    r = client.get("/api/nothing-here")
    assert r.status_code == 404 and r.get_json()["error"]
    big = client.post("/api/ocr", data={"receipt": (io.BytesIO(b"0" * (9 * 1024 * 1024)), "big.png")},
                      content_type="multipart/form-data")
    assert big.status_code == 413 and "8 MB" in big.get_json()["error"]


def test_nfr03_json_api_rejects_form_posts(client):
    reg(client, "Navyaa", "navyaa@example.com")
    r = client.post("/api/groups", data='{"group_name":"X","base_currency":"EUR"}', content_type="text/plain")
    assert r.status_code == 400 and client.get("/api/groups").get_json() == []


def test_fr03_change_password_needs_current_one(client):
    reg(client, "Navyaa", "navyaa@example.com")
    assert client.get("/api/me").get_json()["email"] == "navyaa@example.com"
    bad = client.put("/api/me", json={"full_name": "Navyaa", "current_password": "wrong", "new_password": "newpass1"})
    assert bad.status_code == 403
    ok = client.put("/api/me", json={"full_name": "Navyaa", "current_password": "secret1", "new_password": "newpass1"})
    assert ok.status_code == 200
    client.post("/api/logout")
    assert client.post("/api/login", json={"email": "navyaa@example.com", "password": "newpass1"}).status_code == 200


def test_fr30_personal_dashboard_combined_totals(client):
    gid, ids = setup_group(client)
    add(client, gid, ids, 90, "EUR", "Navyaa")
    g2 = client.post("/api/groups", json={"group_name": "Goa", "base_currency": "USD"}).get_json()["group_id"]
    me2 = client.get(f"/api/groups/{g2}").get_json()["my_member_id"]
    client.post(f"/api/groups/{g2}/expenses", json=dict(description="Taxi", category="Transport", amount=46, currency="USD",
                paid_by=me2, split_type="equal", participants=[{"member_id": me2, "value": 1}]))
    d = client.get("/api/me/dashboard?currency=EUR").get_json()
    assert len(d["groups"]) == 2 and d["totals"]["currency"] == "EUR"
    assert abs(d["totals"]["paid"] - (90 + 46 * 0.92)) < 0.01
    assert abs(d["totals"]["net"] - 60) < 0.01
    cats = {c["category"]: c["total"] for c in d["by_category"]}
    assert cats["Food"] == 30.0 and abs(cats["Transport"] - 46 * 0.92) < 0.01
    assert [r["description"] for r in d["recent"]] == ["Taxi", "x"]


def test_fr31_summary_by_participant(client):
    gid, ids = setup_group(client)
    add(client, gid, ids, 90, "EUR", "Navyaa")
    p = {x["name"].split(" (")[0]: x for x in client.get(f"/api/groups/{gid}/dashboard").get_json()["by_participant"]}
    assert p["Navyaa"]["paid"] == 90 and p["Arjun"]["spent"] == 30 and p["Arjun"]["net"] == -30
    assert client.get(f"/api/groups/{gid}/dashboard").get_json()["by_day"] == [{"day": "2026-10-10", "total": 90.0}]


def test_guest_sees_settlement_history(client):
    gid, ids = setup_group(client)
    add(client, gid, ids, 90, "EUR", "Navyaa")
    client.post(f"/api/groups/{gid}/settlements", json={"payer_id": ids["Arjun"], "receiver_id": ids["Navyaa"], "amount": 10})
    link = [m for m in client.get(f"/api/groups/{gid}").get_json()["members"] if m["is_guest"]][0]["guest_link"]
    d = client.application.test_client().get("/api/" + link.lstrip("/")).get_json()
    assert d["settlements"][0]["amount_base"] == 10


@pytest.mark.parametrize("text,amount", [
    ("TOTAL 45.S0", 45.5), ("Total EUR 1O.00", 10.0), ("TOTAL: 1,2B4.50", 1284.5), ("Sales tax\nTotal 12.00", 12.0),
])
def test_fr20_ocr_lookalike_digits_are_repaired(text, amount):
    assert parse_receipt_text(text)["amount"] == amount


def test_fr20_receipt_photo_is_cleaned_before_ocr():
    from PIL import Image, ImageDraw
    from modules.ocr import prepare_image
    photo = Image.new("RGB", (400, 600), (205, 200, 190))
    ImageDraw.Draw(photo).text((40, 500), "TOTAL 45.50", fill=(120, 120, 120))
    gray, bw = prepare_image(photo)
    assert gray.mode == "L" and gray.width >= 1200
    assert [i for i, n in enumerate(bw.histogram()) if n] == [0, 255]


@pytest.fixture
def pages_client(tmp_path):
    app = create_app(db_path=str(tmp_path / "p.db"), testing=True, frontend_origin="https://navyaa0025.github.io",
                     database_url=fresh_postgres())
    app.config["UPLOAD_DIR"] = str(tmp_path)
    yield app.test_client()
    close_pool(app)


def test_pages_frontend_can_use_the_api(pages_client):
    page = {"Origin": "https://navyaa0025.github.io"}
    pre = pages_client.options("/api/groups", headers={**page, "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 204 and pre.headers["Access-Control-Allow-Origin"] == page["Origin"]
    r = pages_client.post("/api/register", json={"full_name": "N", "email": "n@example.com", "password": "secret1"}, headers=page)
    assert r.status_code == 201 and r.headers["Access-Control-Allow-Credentials"] == "true"
    cookie = r.headers["Set-Cookie"]
    assert "SameSite=None" in cookie and "Secure" in cookie and "HttpOnly" in cookie


def test_nfr03_other_websites_are_blocked(pages_client):
    evil = {"Origin": "https://evil.example"}
    pages_client.post("/api/register", json={"full_name": "N", "email": "n@example.com", "password": "secret1"})
    r = pages_client.post("/api/groups", json={"group_name": "X", "base_currency": "EUR"}, headers=evil)
    assert r.status_code == 403 and "Access-Control-Allow-Origin" not in r.headers
    assert pages_client.options("/api/groups", headers=evil).status_code == 403
    assert pages_client.get("/api/groups").get_json() == []


def test_same_server_page_keeps_strict_cookie(client):
    r = client.post("/api/register", json={"full_name": "N", "email": "n@example.com", "password": "secret1"},
                    headers={"Origin": "http://localhost"})
    assert r.status_code == 201 and "SameSite=Lax" in r.headers["Set-Cookie"]
    assert client.get("/guest/abc").headers["Location"].endswith("/#guest=abc")


from modules.ocr import boxes_to_lines


def test_fr20_tilted_boxes_are_regrouped_into_receipt_lines():
    import math
    rows = [("SUBTOTAL", "49.50"), ("Tax", "4.86"), ("TOTAL EUR", "53.50"), ("Card", "53.50")]
    boxes, texts, tilt = [], [], math.radians(-4)
    for i, (label, price) in enumerate(rows):
        for x0, text, w in ((40, label, 150), (520, price, 70)):
            y0 = 100 + 40 * i
            pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + 22), (x0, y0 + 22)]
            rot = [(x * math.cos(tilt) - y * math.sin(tilt), x * math.sin(tilt) + y * math.cos(tilt)) for x, y in pts]
            boxes.append(rot); texts.append(text)
    lines = boxes_to_lines(boxes, texts).splitlines()
    assert lines == ["SUBTOTAL  49.50", "Tax  4.86", "TOTAL EUR  53.50", "Card  53.50"]
    assert parse_receipt_text("\n".join(lines))["amount"] == 53.5


@pytest.mark.parametrize("text,amount", [
    ("Zwischensumme 40,00\nGesamt 44,50 EUR", 44.5), ("Sous-total 20,00\nMontant 23,00", 23.0),
    ("Total items: 4\nSubtotal 24.29\nTotal tax 2.16\nTOTAL $26.45", 26.45), ("Importe 12,80 €", 12.8),
])
def test_fr20_total_keywords_in_several_languages(text, amount):
    assert parse_receipt_text(text)["amount"] == amount


def test_fr20_rapidocr_reads_a_generated_receipt():
    pytest.importorskip("rapidocr")
    from PIL import Image, ImageDraw, ImageFont
    from modules.ocr import run_ocr
    img = Image.new("RGB", (640, 520), (240, 236, 226))
    d, f = ImageDraw.Draw(img), ImageFont.load_default(size=30)
    for i, (l, r) in enumerate([("CAFE ROMA", ""), ("Cappuccino", "3,50"), ("Cornetto", "2,00"),
                                ("Subtotal", "5,50"), ("TOTAL EUR", "45,50"), ("Cash", "50,00")]):
        d.text((40, 40 + 70 * i), l, font=f, fill=(40, 40, 40))
        if r:
            d.text((470, 40 + 70 * i), r, font=f, fill=(40, 40, 40))
    buf = io.BytesIO(); img.rotate(3, expand=True, fillcolor=(80, 80, 80)).save(buf, "JPEG", quality=80)
    res = run_ocr(buf.getvalue())
    assert res["engine"] == "rapidocr" and res["amount"] == 45.5 and res["currency"] == "EUR"


def test_fr20_receipt_details_items_shop_date_category():
    text = ("TRATTORIA DA MARIO\nVia Roma 12\n24/10/2026 20:41\n2x Pizza Margherita  18,00\nLasagne  12,50\n"
            "Tiramisu  6,50\nSUBTOTALE  37,00\nCoperto 2x  4,00\nTOTALE EUR  41,00\nCarta di credito  41,00")
    r = parse_receipt_text(text)
    assert r["amount"] == 41.0 and r["currency"] == "EUR" and r["merchant"] == "Trattoria Da Mario"
    assert r["date"] == "2026-10-24" and r["category"] == "Food"
    assert r["items"] == [{"name": "2x Pizza Margherita", "price": 18.0}, {"name": "Lasagne", "price": 12.5},
                          {"name": "Tiramisu", "price": 6.5}]


@pytest.mark.parametrize("text,cur,expected", [
    ("10/22/2026", "USD", "2026-10-22"), ("03/04/2026", "EUR", "2026-04-03"), ("03/04/2026", "USD", "2026-03-04"),
    ("2026-10-21 14:02", None, "2026-10-21"), ("24 Oct 2026", None, "2026-10-24"), ("Oct 5, 2026", None, "2026-10-05"),
    ("12/12/1999", None, None),
])
def test_fr20_receipt_dates(text, cur, expected):
    from modules.ocr import receipt_date
    assert receipt_date(text, cur) == expected


def test_fr16_fr18_item_by_item_split(client):
    gid, ids = setup_group(client)
    n, a, c = ids["Navyaa"], ids["Arjun"], ids["Chandana"]
    items = [{"name": "Pizza", "price": 18, "members": [n, a]},
             {"name": "Lasagne", "price": 12.5, "members": [c]},
             {"name": "Wine", "price": 9.5, "members": [n, a, c]}]
    r = client.post(f"/api/groups/{gid}/expenses", json=dict(description="Trattoria", category="Food", amount=44, currency="EUR",
                    paid_by=n, split_type="item", items=items, participants=[]))
    assert r.status_code == 201, r.get_json()
    e = r.get_json()
    shares = {s["member_id"]: s["share_base"] for s in e["splits"]}


    assert sorted([shares[n], shares[a]]) == [13.38, 13.39] and shares[c] == 17.23 and round(sum(shares.values()), 2) == 44.0
    assert e["split_type"] == "item" and [i["members"] for i in e["items"]] == [[n, a], [c], [n, a, c]]
    items[2]["members"] = [c]
    e2 = client.put(f"/api/expenses/{e['expense_id']}", json=dict(description="Trattoria", category="Food", amount=44,
                    currency="EUR", paid_by=n, split_type="item", items=items, participants=[])).get_json()
    assert {s["member_id"]: s["share_base"] for s in e2["splits"]}[c] == 24.2 and len(e2["items"]) == 3


@pytest.mark.parametrize("items,msg", [
    ([], "at least one item"), ([{"name": "Pizza", "price": 10, "members": []}], "who had"),
    ([{"name": "Pizza", "price": "x", "members": [1]}], "price"), ([{"name": "Pizza", "price": 10, "members": [999]}], "not in this group"),
    ([{"name": "Pizza", "price": 500, "members": [1]}], "far more"),
])
def test_fr16_item_split_rejects_bad_items(client, items, msg):
    gid, ids = setup_group(client)
    for it in items:
        it["members"] = [ids["Navyaa"] if m == 1 else m for m in it["members"]]
    r = client.post(f"/api/groups/{gid}/expenses", json=dict(description="x", category="Food", amount=44, currency="EUR",
                    paid_by=ids["Navyaa"], split_type="item", items=items, participants=[]))
    assert r.status_code == 400 and msg in r.get_json()["error"]
    assert client.get(f"/api/groups/{gid}/expenses").get_json() == []


def test_fr20_unreadable_item_price_is_recovered_from_the_subtotal():
    text = ("TRATTORIA DA MARIO\n2x Pizza Margherita  18,00\nLasagne  12,50\nTiramisu  6,50\nVino rosso 0.5L  006\n"
            "SUBTOTALE  46,00\nCoperto 3x  6,00\nTOTALE EUR  52,00")
    items = parse_receipt_text(text)["items"]
    assert [i["name"] for i in items] == ["2x Pizza Margherita", "Lasagne", "Tiramisu", "Vino rosso 0.5L"]
    assert items[-1]["price"] == 9.0
    no_subtotal = parse_receipt_text(text.replace("SUBTOTALE  46,00\n", ""))["items"]
    assert no_subtotal[-1] == {"name": "Vino rosso 0.5L", "price": None}
