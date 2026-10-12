"""Shared bill presentation for text replies and cards."""

from decimal import Decimal


def bill_label(date, category, description, amount):
    amount_text = format(Decimal(str(amount)), "f")
    if "." in amount_text:
        amount_text = amount_text.rstrip("0").rstrip(".")
    fields = [str(date).replace("-", "/")]
    fields.extend(category[key] for key in ("level1", "level2", "level3") if category.get(key))
    fields.extend((description, amount_text + "元"))
    return "｜".join(" ".join(value.splitlines()) for value in fields)
