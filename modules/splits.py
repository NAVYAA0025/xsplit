import math


def allocate(total_cents, weights):
    tw = sum(weights)
    raw = [total_cents * w / tw for w in weights]
    base = [math.floor(r + 1e-9) for r in raw]
    rem = total_cents - sum(base)
    order = sorted(range(len(weights)), key=lambda i: raw[i] - base[i], reverse=True)
    for i in order[:rem]:
        base[i] += 1
    return base


def _values(parts, label):
    try:
        weights = [float(p.get("value", 0)) for p in parts]
    except (TypeError, ValueError):
        raise ValueError(f"Enter a valid {label} for every selected participant.")
    if any(not math.isfinite(w) for w in weights):
        raise ValueError(f"Enter a valid {label} for every selected participant.")
    if any(w < 0 for w in weights):
        raise ValueError(f"{label.capitalize()}s cannot be negative.")
    return weights


def clean_items(items, member_ids):
    if not isinstance(items, list) or not items:
        raise ValueError("Add at least one item.")
    if len(items) > 60:
        raise ValueError("Too many items (max 60).")
    out = []
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            raise ValueError("Invalid item list.")
        name = str(it.get("name") or "").strip()[:60] or f"Item {i}"
        try:
            price = float(it.get("price"))
        except (TypeError, ValueError):
            raise ValueError(f"Enter a price for “{name}”.")
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"The price of “{name}” must be greater than zero.")
        members = it.get("members")
        if not isinstance(members, list) or not members:
            raise ValueError(f"Choose who had “{name}”.")
        if any(m not in member_ids for m in members) or len(set(members)) != len(members):
            raise ValueError(f"Someone chosen for “{name}” is not in this group.")
        out.append({"name": name, "price": round(price, 2), "members": members})
    return out


def build_item_splits(amount, rate, items):
    item_total = sum(it["price"] for it in items)
    if item_total - amount > 0.01 and item_total > amount * 2:
        raise ValueError(f"The items add up to {item_total:.2f}, far more than the total of {amount:.2f}. Check the prices.")
    weights = {}
    for it in items:
        for m in it["members"]:
            weights[m] = weights.get(m, 0) + it["price"] / len(it["members"])
    ids = list(weights)
    orig = allocate(round(amount * 100), [weights[m] for m in ids])
    base = allocate(round(amount * rate * 100), [weights[m] for m in ids])
    return [(ids[i], orig[i] / 100, base[i] / 100, None) for i in range(len(ids))]


def build_splits(amount, rate, split_type, parts):
    if not parts:
        raise ValueError("Select at least one participant.")
    ids = [p["member_id"] for p in parts]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate participant in split.")
    if split_type == "equal":
        weights, pcts = [1] * len(parts), [None] * len(parts)
    elif split_type == "unequal":
        weights = _values(parts, "share")
        if abs(sum(weights) - amount) > 0.01:
            raise ValueError(f"Unequal shares total {sum(weights):.2f} but the expense is {amount:.2f}.")
        pcts = [None] * len(parts)
    elif split_type == "percent":
        weights = _values(parts, "percentage")
        if abs(sum(weights) - 100) > 0.01:
            raise ValueError(f"Percentages total {sum(weights):.2f}, they must total 100.")
        pcts = weights
    else:
        raise ValueError("Unknown split type.")
    if sum(weights) <= 0:
        raise ValueError("Shares must be greater than zero.")
    orig = allocate(round(amount * 100), weights)
    base = allocate(round(amount * rate * 100), weights)
    return [(ids[i], orig[i] / 100, base[i] / 100, pcts[i]) for i in range(len(parts))]
