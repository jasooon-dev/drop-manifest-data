#!/usr/bin/env python3
"""
Drop Manifest daily scraper.

Runs with full internet access (GitHub Actions). Checks each tracked brand's
Shopify storefront for genuine markdowns, verifies stock, and either builds a
Sale Check item list or a set of Daily Picks (apparel only, no shoes/no
accessories) for brands with nothing on sale. Writes everything to data.json,
including base64-embedded product photos, so the downstream Claude routine
(which runs in a network-restricted sandbox) never has to make its own
outbound web requests -- it only needs to read this one file from the repo.
"""

import base64
import json
import re
import sys
import time
from datetime import date

import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
HEADERS = {"User-Agent": UA}
PRICE_CAP = 300
TIMEOUT = 20

EXCLUDE_KEYWORDS = [
    "shoe", "sneaker", "footwear", "hat", "cap", "beanie", "bag", "belt",
    "jewelry", "jewellery", "sunglass", "eyewear", "sock", "keychain",
    "scarf", "candle", "accessor", "pin", "sticker", "durag", "bandana",
    "fob", "boutonniere", "snapback", "fitted", "wallet",
]

# All the letter-size tokens we know how to recognize across variant option
# values, and the subset the user can actually wear. A product whose variants
# use none of these tokens at all (e.g. "O/S", numeric waist sizes, a
# color-only option) is treated as size-agnostic and never filtered out.
SIZE_TOKENS = {"XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "2XL", "3XL"}
WEARABLE_SIZES = {"XS", "S"}


def _size_tokens_in(value):
    if not value:
        return set()
    parts = re.split(r"[\/\s]+", value.strip().upper())
    return {p for p in parts if p in SIZE_TOKENS}


def product_size_tokens(product):
    """All distinct recognized size tokens used anywhere across this product's variants."""
    sizes = set()
    for v in product.get("variants", []):
        for opt in (v.get("option1"), v.get("option2"), v.get("option3")):
            sizes |= _size_tokens_in(opt)
    return sizes


def variant_is_wearable(variant):
    for opt in (variant.get("option1"), variant.get("option2"), variant.get("option3")):
        if _size_tokens_in(opt) & WEARABLE_SIZES:
            return True
    return False


def variant_fits(product_sizes, variant):
    """True if this variant is wearable, OR the product has no recognizable
    size dimension at all (one-size items aren't filtered)."""
    if not product_sizes:
        return True
    return variant_is_wearable(variant)

BRANDS = [
    {"key": "ryoko_rain", "name": "Ryoko Rain", "domain": "ryokorain.com",
     "shop_link": "https://ryokorain.com/collections/all"},
    {"key": "chinatown_market", "name": "Chinatown Market", "domain": "www.chinatownmarket.com",
     "shop_link": "https://www.chinatownmarket.com/", "homepage_scrape": True},
    {"key": "aries_arise", "name": "Aries Arise", "domain": "us.ariesarise.com",
     "sale_handle": "sale", "shop_link": "https://us.ariesarise.com/collections/sale"},
    {"key": "denim_tears", "name": "Denim Tears", "domain": "denimtears.com",
     "shop_link": "https://denimtears.com/collections/shop-all"},
    {"key": "cpfm", "name": "Cactus Plant Flea Market", "domain": "cactusplantfleamarket.com",
     "shop_link": "https://cactusplantfleamarket.com/"},
    {"key": "online_ceramics", "name": "Online Ceramics", "domain": "online-ceramics.com",
     "shop_link": "https://online-ceramics.com/collections/shop-all"},
    {"key": "born_x_raised", "name": "Born X Raised", "domain": "bornxraised.com",
     "shop_link": "https://bornxraised.com/collections/shop"},
    {"key": "golf_wang", "name": "Golf Wang", "domain": "www.golfwang.com",
     "shop_link": "https://www.golfwang.com/collections/all"},
]


def is_excluded(title, product_type):
    t = (title or "").lower()
    p = (product_type or "").lower()
    return any(k in t or k in p for k in EXCLUDE_KEYWORDS)


def fetch_json(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def fetch_all_products(domain, max_pages=5):
    """Paginate /collections/all/products.json, dedup by id."""
    seen = {}
    for page in range(1, max_pages + 1):
        data = fetch_json(f"https://{domain}/collections/all/products.json?limit=250&page={page}")
        if not data or not data.get("products"):
            break
        for p in data["products"]:
            seen[p["id"]] = p
        if len(data["products"]) < 250:
            break
    return list(seen.values())


def product_url(domain, product):
    handle = product.get("handle")
    return f"https://{domain}/products/{handle}" if handle else f"https://{domain}/"


def fetch_image_b64(url):
    if not url:
        return None, None
    try:
        sep = "&" if "?" in url else "?"
        r = requests.get(f"{url}{sep}width=600", headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200 or not r.content:
            return None, None
        mime = "image/png" if url.lower().endswith(".png") else "image/jpeg"
        return base64.b64encode(r.content).decode("ascii"), mime
    except Exception:
        return None, None


def genuine_discounts(products, require_available=False, require_fit=True):
    """Return list of (product, variant, price, compare) with a real markdown.

    When require_fit is set, a variant only counts if it's in stock in a
    wearable size (XS/S) -- unless the product has no recognizable size
    dimension at all, in which case it's never filtered on size.
    """
    out = []
    for p in products:
        if is_excluded(p.get("title"), p.get("product_type")):
            continue
        sizes = product_size_tokens(p) if require_fit else set()
        for v in p.get("variants", []):
            price = float(v.get("price") or 0)
            compare = v.get("compare_at_price")
            compare = float(compare) if compare else None
            if compare and compare > price and 0 < price <= PRICE_CAP:
                if require_available and not v.get("available"):
                    continue
                if require_fit and not variant_fits(sizes, v):
                    continue
                out.append((p, v, price, compare))
                break
    return out


def in_stock_apparel(products, limit=None, require_fit=True):
    """Apparel-only, in-stock, under price cap, in a wearable size. Used for Daily Picks."""
    out = []
    for p in products:
        if is_excluded(p.get("title"), p.get("product_type")):
            continue
        sizes = product_size_tokens(p) if require_fit else set()
        avail_variants = [v for v in p.get("variants", []) if v.get("available") is True]
        if require_fit:
            avail_variants = [v for v in avail_variants if variant_fits(sizes, v)]
        if not avail_variants:
            continue
        price_ok = None
        for v in avail_variants:
            price = float(v.get("price") or 0)
            if 0 < price <= PRICE_CAP:
                price_ok = v
                break
        if not price_ok:
            continue
        img = p["images"][0]["src"] if p.get("images") else None
        out.append({"title": p["title"], "price": float(price_ok["price"]), "image_url": img,
                     "product": p})
    if limit:
        out = out[:limit]
    return out


def check_chinatown_market():
    """Special-cased: this store has gone password-gated before. Detect it."""
    try:
        r = requests.get("https://www.chinatownmarket.com/", headers=HEADERS,
                          timeout=TIMEOUT, allow_redirects=True)
        if "/password" in r.url:
            return {"status": "unavailable", "sub": "Storefront down",
                    "note": "Site is password-gated right now.",
                    "link": "https://www.chinatownmarket.com/"}
    except Exception:
        return {"status": "unavailable", "sub": "Storefront down",
                "note": "Site could not be reached.",
                "link": "https://www.chinatownmarket.com/"}

    products = fetch_all_products("www.chinatownmarket.com", max_pages=2)
    if not products:
        return {"status": "unavailable", "sub": "Storefront down",
                "note": "Site could not be reached.",
                "link": "https://www.chinatownmarket.com/"}

    discounts = genuine_discounts(products, require_available=True)
    if not discounts:
        return {"status": "none", "sub": "No live sale section",
                "note": "Nothing at the moment.",
                "link": "https://www.chinatownmarket.com/"}

    items = []
    for p, v, price, compare in discounts[:6]:
        img_url = p["images"][0]["src"] if p.get("images") else None
        b64, mime = fetch_image_b64(img_url)
        items.append({"name": p["title"], "was": compare, "now": price,
                       "image_b64": b64, "image_mime": mime,
                       "link": product_url("www.chinatownmarket.com", p)})
    return {"status": "active", "sub": "Sale-tagged items", "items": items,
            "link": "https://www.chinatownmarket.com/"}


def process_sale_brand(brand):
    domain = brand["domain"]

    if brand.get("homepage_scrape"):
        return check_chinatown_market()

    products = None
    if brand.get("sale_handle"):
        data = fetch_json(f"https://{domain}/collections/{brand['sale_handle']}/products.json?limit=250")
        if data and data.get("products"):
            products = data["products"]
            # paginate further if a full page came back
            page = 2
            while True:
                more = fetch_json(f"https://{domain}/collections/{brand['sale_handle']}/products.json?limit=250&page={page}")
                if not more or not more.get("products"):
                    break
                products.extend(more["products"])
                if len(more["products"]) < 250:
                    break
                page += 1
                if page > 6:
                    break

    if products is None:
        products = fetch_all_products(domain)

    if not products:
        return {"status": "unavailable", "sub": "Storefront down",
                "note": "Site could not be reached.", "link": brand["shop_link"]}

    discounts = genuine_discounts(products, require_available=True)

    if not discounts:
        return {"status": "none", "sub": "No live sale section",
                "note": "Nothing at the moment.", "link": brand["shop_link"]}

    # Rank by absolute savings, keep unique titles, cap at 6 for a readable card.
    seen_titles = set()
    ranked = sorted(discounts, key=lambda d: -(d[3] - d[2]))
    picked = []
    for p, v, price, compare in ranked:
        if p["title"] in seen_titles:
            continue
        seen_titles.add(p["title"])
        picked.append((p, v, price, compare))
        if len(picked) >= 6:
            break

    items = []
    for p, v, price, compare in picked:
        img_url = p["images"][0]["src"] if p.get("images") else None
        b64, mime = fetch_image_b64(img_url)
        items.append({"name": p["title"], "was": compare, "now": price,
                       "image_b64": b64, "image_mime": mime,
                       "link": product_url(domain, p)})

    return {"status": "active", "sub": "Sale collection", "items": items, "link": brand["shop_link"]}


def build_daily_picks(brand):
    domain = brand["domain"]
    products = fetch_all_products(domain)
    if not products:
        return None
    candidates = in_stock_apparel(products)
    if not candidates:
        return None
    # Favor variety: sort by price descending to surface more interesting/statement pieces first,
    # but this is a light heuristic, not a hard rule.
    candidates.sort(key=lambda c: -c["price"])
    picked = candidates[:3]
    items = []
    for c in picked:
        b64, mime = fetch_image_b64(c["image_url"])
        items.append({"name": c["title"], "now": c["price"], "image_b64": b64, "image_mime": mime,
                       "link": product_url(domain, c["product"])})
    note = None
    if len(picked) < 3:
        note = f"Only {len(picked)} in-stock, non-accessory item{'s' if len(picked) != 1 else ''} in the whole catalog right now."
    return {"link": brand["shop_link"], "items": items, "note": note}


def main():
    result = {"date": date.today().isoformat(), "brands": {}, "daily_picks": {}}

    for brand in BRANDS:
        print(f"Checking {brand['name']}...", file=sys.stderr)
        try:
            info = process_sale_brand(brand)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            info = {"status": "unavailable", "sub": "Storefront down",
                     "note": "Could not check this brand today.", "link": brand["shop_link"]}
        result["brands"][brand["key"]] = {"name": brand["name"], **info}
        print(f"  -> {info['status']}", file=sys.stderr)
        time.sleep(0.5)

    for brand in BRANDS:
        status = result["brands"][brand["key"]]["status"]
        if status != "none":
            continue
        print(f"Building daily picks for {brand['name']}...", file=sys.stderr)
        try:
            picks = build_daily_picks(brand)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            picks = None
        if picks and picks["items"]:
            result["daily_picks"][brand["key"]] = {"name": brand["name"], **picks}
        time.sleep(0.5)

    with open("data.json", "w") as f:
        json.dump(result, f, indent=2)
    print("Wrote data.json", file=sys.stderr)


if __name__ == "__main__":
    main()
