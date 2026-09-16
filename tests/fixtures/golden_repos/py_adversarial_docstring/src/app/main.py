from app.discount import apply_discount


def checkout(price, percent_off=10):
    return apply_discount(price, percent_off)
