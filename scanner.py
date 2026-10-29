"""Sneaker deal scanner. Single file: scans stores, checks resale prices, applies coupons, writes deals.json."""
from dataclasses import dataclass, field, asdict
import re
import json, time, pathlib
import requests
import sqlite3, time
import os, requests
import html as htmllib, re
import csv, io
import json, os, re
from urllib.parse import quote, urljoin
import csv, pathlib
import os
import json, sys, time, pathlib, yaml
from urllib.parse import quote

# ---- models --------------------------------------------
@dataclass
class Offer:
    store: str
    title: str
    url: str
    price: float
    compare_at: float          # original / "was" price; 0 if not on sale
    sku: str = ""              # style code, e.g. DD1391-100 (best key for matching)
    brand: str = ""
    sizes: list = field(default_factory=list)
    image: str = ""

    @property
    def discount_pct(self):
        return round((1 - self.price / self.compare_at) * 100) if self.compare_at > self.price else 0

@dataclass
class Resale:
    last_sale: float = 0.0
    avg_30d: float = 0.0
    sales_30d: int = 0
    lowest_ask: float = 0.0

    @property
    def value(self):
        # conservative: the lower of recent average and last sale
        vals = [v for v in (self.avg_30d, self.last_sale) if v > 0]
        return min(vals) if vals else 0.0

def to_dict(o):
    return asdict(o)


# ---- skus ----------------------------------------------
# Common style-code shapes: Nike/Jordan DD1391-100, Adidas IG1234 / GW1234, NB U990TG6, ASICS 1201A019-020
PATTERNS = [
    r"\b[A-Z]{2}\d{4}-\d{3}\b",
    r"\b[A-Z]{1,2}\d{4,5}[A-Z]?\b",
    r"\b\d{4}[A-Z]\d{3}-\d{3}\b",
]
def find_sku(*texts):
    blob = " ".join(t for t in texts if t).upper()
    for p in PATTERNS:
        m = re.search(p, blob)
        if m:
            return m.group(0)
    return ""

def norm(sku):
    return re.sub(r"[^A-Z0-9]", "", (sku or "").upper())


# ---- http ----------------------------------------------
UA = "Mozilla/5.0 (compatible; PersonalDealScanner/1.0)"
_last = {}

def get_json(url, headers=None, delay=1.5):
    if url.startswith("file:"):
        p = pathlib.Path(url[5:])
        return json.loads(p.read_text()) if p.exists() else None
    host = url.split("/")[2]
    wait = delay - (time.time() - _last.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last[host] = time.time()
    r = requests.get(url, headers={"User-Agent": UA, **(headers or {})}, timeout=20)
    if r.status_code != 200:
        return None
    return r.json()

def get_text(url, headers=None):
    if url.startswith("file:"):
        return pathlib.Path(url[5:]).read_text()
    r = requests.get(url, headers={"User-Agent": UA, **(headers or {})}, timeout=60)
    r.raise_for_status()
    return r.text


# ---- scoring -------------------------------------------
def profit(buy, resale_value, c, buy_shipping=0.0):
    """Net profit if you buy at `buy` and sell at `resale_value` after marketplace costs."""
    cost = buy * (1 + c["sales_tax"]) + buy_shipping
    net_sale = resale_value * (1 - c["seller_fee_pct"]) - c["seller_flat_fee"] - c["shipping"]
    return round(net_sale - cost, 2), round((net_sale - cost) / cost * 100, 1) if cost else 0

def score(roi, sales_30d, discount, reliability):
    s = min(max(roi, 0) / 60, 1) * 45        # return on money
    s += min(sales_30d / 40, 1) * 30          # how fast it actually sells
    s += min(discount / 50, 1) * 15           # depth of sale
    s += reliability                          # 0-10 per store
    return round(s)


# ---- db ------------------------------------------------
def connect(path):
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE IF NOT EXISTS seen(url TEXT PRIMARY KEY, price REAL, first_seen REAL,
                  last_seen REAL, alerted_price REAL)""")
    return db

def is_new_or_dropped(db, url, price):
    row = db.execute("SELECT price, alerted_price FROM seen WHERE url=?", (url,)).fetchone()
    now = time.time()
    if row is None:
        db.execute("INSERT INTO seen VALUES(?,?,?,?,NULL)", (url, price, now, now))
        return True
    db.execute("UPDATE seen SET price=?, last_seen=? WHERE url=?", (price, now, url))
    return row[1] is None or price < row[1] - 0.5

def mark_alerted(db, url, price):
    db.execute("UPDATE seen SET alerted_price=? WHERE url=?", (price, url))


# ---- alerts --------------------------------------------
def push(cfg, deals):
    """Push via ntfy.sh (free, install the ntfy app, subscribe to your topic). Set NTFY_TOPIC or config."""
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic")
    if not topic or not deals:
        return False
    lines = [f"{d['title']} · ${d['price']:.0f} at {d['store']} · +${d['profit']:.0f} ({d['roi']:.0f}% ROI)" for d in deals[:8]]
    body = "\n".join(lines) + (f"\n+{len(deals)-8} more" if len(deals) > 8 else "")
    requests.post(f"https://ntfy.sh/{topic}", data=body.encode(), timeout=15,
                  headers={"Title": f"{len(deals)} profitable sneaker deal(s)", "Click": deals[0]["url"]})
    return True


# ---- coupons -------------------------------------------
STOP = {"FREE", "SHIPPING", "CHECKOUT", "ORDER", "ORDERS", "ONLINE", "TODAY", "SAVE", "ONLY", "SALE",
        "OFF", "MEMBERS", "EMAIL", "ENTER", "APPLIED", "AUTOMATICALLY", "DISCOUNT", "PROMO", "CODE", "WITH"}
CODE_RES = [
    re.compile(r"\b(?:promo\s+|coupon\s+|discount\s+)?code\s*[:\-]?\s*[\"'“”‘’]?([A-Za-z0-9]{4,20})\b", re.I),
    re.compile(r"\buse\s+[\"'“”‘’]?([A-Z0-9]{4,20})\b"),
]
AMOUNT = re.compile(r"\$\s?(\d+(?:\.\d+)?)\s*off", re.I)
PCT = re.compile(r"(\d{1,2})\s*%\s*off", re.I)
SPLIT = re.compile(r"[|•!]|\.\s")
MIN = re.compile(r"(?:over|orders?\s+of|minimum|min\.?|spend)\s*\$\s?(\d+)|\$\s?(\d+)\s*\+", re.I)

def visible_text(page):
    page = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", page)))

def segment(text, a, b):
    """The banner phrase around a match, bounded by | . ! so one code never borrows another's amount."""
    start = max([d.end() for d in SPLIT.finditer(text, 0, a)] or [0])
    nxt = SPLIT.search(text, b)
    return text[start: nxt.start() if nxt else len(text)]

def parse(text):
    found = {}
    for rx in CODE_RES:
        for m in rx.finditer(text):
            code = m.group(1)
            if code != code.upper() or code in STOP or not re.search(r"[A-Z]", code):
                continue
            win = segment(text, m.start(), m.end())
            amt, pct, mn = AMOUNT.search(win), PCT.search(win), MIN.search(win)
            if not (amt or pct):
                continue
            c = {"code": code, "auto": True}
            if amt:
                c["amount"] = float(amt.group(1))
            elif int(pct.group(1)) <= 60:
                c["pct"] = float(pct.group(1))
            else:
                continue
            if mn:
                c["min_spend"] = float(mn.group(1) or mn.group(2))
            if code not in found:
                found[code] = c
    return list(found.values())

def detect(store):
    """Coupons currently advertised on this store's homepage; [] on any failure."""
    try:
        url = store.get("homepage_url") or store.get("base_url") or (store.get("pages") or [""])[0]
        tpl = store.get("fetch_url")
        if tpl and not store.get("homepage_url"):
            from urllib.parse import quote
            import os
            url = tpl.format(url=quote(url, safe=""), key=os.environ.get(store.get("api_key_env", "SCRAPER_API_KEY"), ""))
        return parse(visible_text(get_text(url)))
    except Exception:
        return []

def add_auto_coupons(cfg):
    for s in cfg["stores"]:
        if s["type"] == "feed" or not s.get("auto_coupons", True):
            continue
        found = detect(s)
        known = {c["code"] for c in s.get("coupons", [])}
        s["coupons"] = s.get("coupons", []) + [c for c in found if c["code"] not in known]
        if found:
            print(f"  {s['name']}: banner codes {', '.join(c['code'] for c in found)}")


# ---- sources/shopify -----------------------------------
def scan_shopify(cfg):
    name = cfg["name"]
    tpl = cfg.get("products_url") or cfg["base_url"].rstrip("/") + "/collections/{collection}/products.json?limit=250&page={page}"
    collection = cfg.get("collection", "sale")
    out = []
    for page in range(1, cfg.get("max_pages", 4) + 1):
        data = get_json(tpl.format(collection=collection, page=page))
        prods = (data or {}).get("products") or []
        if not prods:
            break
        for p in prods:
            sizes, price, was = [], None, 0.0
            for v in p.get("variants", []):
                if not v.get("available"):
                    continue
                vp = float(v["price"])
                price = vp if price is None else min(price, vp)
                cmp_ = float(v.get("compare_at_price") or 0)
                was = max(was, cmp_)
                sizes.append(str(v.get("option1") or v.get("title")))
            if price is None:
                continue
            body = re.sub("<[^>]+>", " ", p.get("body_html") or "")
            sku = find_sku(p.get("title"), " ".join(p.get("tags") or []), body,
                           " ".join(str(v.get("sku") or "") for v in p.get("variants", [])))
            handle = p.get("handle", "")
            imgs = p.get("images") or []
            out.append(Offer(store=name, title=p["title"], url=cfg["base_url"].rstrip("/") + "/products/" + handle,
                             price=price, compare_at=was, sku=sku, brand=p.get("vendor", ""),
                             sizes=sizes, image=imgs[0]["src"] if imgs else ""))
    return out


# ---- sources/feed --------------------------------------
def scan_feed(cfg):
    m = cfg["columns"]
    rows = csv.DictReader(io.StringIO(get_text(cfg["feed_url"])))
    out = []
    for r in rows:
        try:
            price = float(r[m["price"]] or 0)
            was = float(r.get(m.get("was", ""), 0) or 0)
        except ValueError:
            continue
        if price <= 0:
            continue
        sku = r.get(m.get("sku", ""), "") or find_sku(r.get(m["title"]))
        out.append(Offer(store=cfg["name"], title=r[m["title"]], url=r[m["url"]], price=price,
                         compare_at=was, sku=sku, brand=r.get(m.get("brand", ""), ""),
                         image=r.get(m.get("image", ""), "")))
    return out


# ---- sources/scrape ------------------------------------
NAME_KEYS = ("name", "title", "productName", "displayName")
PRICE_KEYS = ("salePrice", "currentPrice", "price", "priceNow", "finalPrice", "nowPrice")
WAS_KEYS = ("listPrice", "originalPrice", "fullPrice", "wasPrice", "regularPrice", "msrp", "compareAtPrice", "initialPrice", "priceWas")
URL_KEYS = ("url", "pdpUrl", "productUrl", "link", "canonicalUrl", "href")
SKU_KEYS = ("styleColor", "styleCode", "styleNumber", "modelNumber", "mpn", "sku", "productCode")
IMG_KEYS = ("image", "imageUrl", "thumbnail", "src")

def num(v):
    """Turn 99.99, "$99.99", or {"value": 99.99} / {"currentPrice": ...} into a float (0 if unknown)."""
    if isinstance(v, bool) or v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"\d[\d,]*\.?\d*", v)
        return float(m.group(0).replace(",", "")) if m else 0.0
    if isinstance(v, dict):
        for k in PRICE_KEYS + ("value", "amount"):
            if k in v and num(v[k]):
                return num(v[k])
    return 0.0

def first(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, "", {}):
            return d[k]
    return None

def blobs(html):
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
        txt = m.group(1).strip()
        if txt[:1] in "{[":
            try:
                yield json.loads(txt)
            except ValueError:
                pass

def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)

def offers_from_html(html, store, base_url):
    out, seen = [], set()
    for blob in blobs(html):
        for d in walk(blob):
            name = first(d, NAME_KEYS)
            if not isinstance(name, str):
                continue
            offers = d.get("offers")
            if isinstance(offers, list) and offers:
                offers = offers[0]
            src = offers if isinstance(offers, dict) else d
            pv = first(src, PRICE_KEYS)
            price = num(pv)
            was = num(first(src, WAS_KEYS)) or (num(first(pv, WAS_KEYS)) if isinstance(pv, dict) else 0)
            if price <= 0:
                continue
            link = first(d, URL_KEYS) or first(src, URL_KEYS)
            link = urljoin(base_url, link) if isinstance(link, str) else base_url
            sku = first(d, SKU_KEYS)
            sku = sku if isinstance(sku, str) and re.search(r"\d", sku) else find_sku(name, link)
            key = (link, price)
            if key in seen:
                continue
            seen.add(key)
            img = first(d, IMG_KEYS)
            brand = d.get("brand")
            out.append(Offer(store=store, title=name, url=link, price=price, compare_at=was, sku=sku or "",
                             brand=brand.get("name", "") if isinstance(brand, dict) else (brand or ""),
                             image=img if isinstance(img, str) else ""))
    return out

def scan_scrape(cfg):
    key = os.environ.get(cfg.get("api_key_env", "SCRAPER_API_KEY"), "")
    out = []
    for page_url in cfg["pages"]:
        for page in range(1, cfg.get("max_pages", 3) + 1):
            url = page_url.format(page=page) if "{page}" in page_url else page_url
            tpl = cfg.get("fetch_url", "{url}")
            fetch = tpl.format(url=quote(url, safe="") if tpl != "{url}" else url, key=key)
            html = get_text(fetch)
            found = offers_from_html(html, cfg["name"], url)
            out += found
            if "{page}" not in page_url or not found:
                break
    return out


# ---- resale/csvfile ------------------------------------
class CsvProvider:
    def __init__(self, cfg):
        self.rows = {}
        p = pathlib.Path(cfg["path"])
        if p.exists():
            for r in csv.DictReader(p.open()):
                self.rows[norm(r["sku"])] = Resale(float(r["last_sale"] or 0), float(r["avg_30d"] or 0),
                                                   int(r["sales_30d"] or 0), float(r.get("lowest_ask") or 0))
    def lookup(self, sku, title):
        return self.rows.get(norm(sku))


# ---- resale/api ----------------------------------------
FIELD_MAP = {  # your provider's JSON key -> our field
    "last_sale": "last_sale", "avg_30d": "avg_price_30d", "sales_30d": "sales_30d", "lowest_ask": "lowest_ask",
}

class ApiProvider:
    def __init__(self, cfg):
        self.url = cfg["url"]            # e.g. https://api.example.com/v1/stockx/products?query={sku}
        self.headers = {"Authorization": os.environ.get(cfg.get("api_key_env", "RESALE_API_KEY"), "")}
        self.cache = {}

    def lookup(self, sku, title):
        key = sku or title
        if key in self.cache:
            return self.cache[key]
        data = get_json(self.url.format(sku=sku, title=title), headers=self.headers, delay=0.5)
        item = data[0] if isinstance(data, list) and data else (data or {}).get("data", [{}])[0] if data else None
        res = None
        if item:
            g = lambda k: float(item.get(FIELD_MAP[k]) or 0)
            res = Resale(g("last_sale"), g("avg_30d"), int(g("sales_30d")), g("lowest_ask"))
        self.cache[key] = res
        return res


# ---- run -----------------------------------------------
SOURCES = {"shopify": scan_shopify, "feed": scan_feed, "scrape": scan_scrape}
PROVIDERS = {"csv": CsvProvider, "api": ApiProvider}

def scan_all(cfg):
    offers = []
    for s in cfg["stores"]:
        try:
            found = SOURCES[s["type"]](s)
            print(f"  {s['name']}: {len(found)} items")
            offers += found
        except Exception as e:           # one broken store must not stop the run
            print(f"  {s['name']}: FAILED ({e})", file=sys.stderr)
    return offers

def checkout(o, store):
    """Best single coupon (codes don't stack) plus shipping. Returns (price after coupon, shipping, code, auto_detected)."""
    best, code, auto = o.price, "", False
    for c in store.get("coupons", []):
        if o.price < c.get("min_spend", 0):
            continue
        after = o.price - c.get("amount", 0) - o.price * c.get("pct", 0) / 100
        if after < best:
            best, code, auto = max(after, 0), c["code"], bool(c.get("auto"))
    ship = 0.0 if best >= store.get("free_shipping_over", 1e9) else store.get("shipping", 0.0)
    return round(best, 2), ship, code, auto

def evaluate(cfg, offers, provider):
    f, rel = cfg["filters"], {s["name"]: s.get("reliability", 6) for s in cfg["stores"]}
    by_name = {s["name"]: s for s in cfg["stores"]}
    out = []
    for o in offers:
        eff, ship, code, auto = checkout(o, by_name[o.store])
        was = o.compare_at if o.compare_at > o.price else 0
        disc = round((1 - eff / was) * 100) if was else 0
        if disc < f["min_discount"]:
            continue
        if not (f["min_price"] <= eff <= f["max_price"]):
            continue
        res = provider.lookup(o.sku, o.title) if (o.sku or not f["require_sku"]) else None
        if not res or res.value <= 0 or res.sales_30d < f["min_sales_30d"]:
            continue
        p, roi = profit(eff, res.value, cfg["costs"], ship)
        if p < f["min_profit"] or roi < f["min_roi"]:
            continue
        d = to_dict(o)
        d.update(stockx_url="https://stockx.com/search?s=" + quote(o.sku or o.title),
                 price_after_coupon=eff, coupon=code, coupon_auto=auto, shipping=ship,
                 discount=disc, resale=round(res.value, 2), sales_30d=res.sales_30d,
                 profit=p, roi=roi, score=score(roi, res.sales_30d, disc, rel.get(o.store, 6)))
        out.append(d)
    return sorted(out, key=lambda d: d["score"], reverse=True)

def main(path="config.yaml"):
    cfg = yaml.safe_load(open(path))
    print("Scanning stores...")
    offers = scan_all(cfg)
    add_auto_coupons(cfg)
    provider = PROVIDERS[cfg["resale"]["type"]](cfg["resale"])
    deals = evaluate(cfg, offers, provider)
    conn = connect(cfg.get("db_path", "deals.sqlite"))
    fresh = [d for d in deals if is_new_or_dropped(conn, d["url"], d["price"])]
    if fresh and push(cfg.get("alerts", {}), fresh):
        for d in fresh:
            mark_alerted(conn, d["url"], d["price"])
    conn.commit()
    out = pathlib.Path(cfg.get("output", "site/deals.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "scanned": len(offers), "deals": deals}, indent=1))
    print(f"{len(offers)} scanned, {len(deals)} profitable, {len(fresh)} new alerts -> {out}")

if __name__ == "__main__":
    main(*sys.argv[1:])
