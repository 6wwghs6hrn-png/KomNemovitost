#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hlídač komerčních prostor – Klánovice a okolí (cca 5 km).

Co dělá:
  1) Stáhne aktuální nabídky komerčních nemovitostí ze Sreality JSON API.
  2) Vyfiltruje je na okruh do RADIUS_KM od zadaného středu (Klánovice).
  3) Porovná se seznamem už viděných nabídek -> najde NOVINKY.
  4) Vygeneruje statickou stránku docs/index.html (dnešní novinky + archiv).
  5) Uloží stav, aby příště poznal, co je nové.

Spouští se 1x denně přes GitHub Actions; stránku publikuje GitHub Pages.
"""

import json
import math
import re
import time
import html
import datetime
import pathlib
import urllib.parse

import requests

# ──────────────────────────────────────────────────────────────────────────
#  KONFIGURACE  (tohle si uprav podle sebe)
# ──────────────────────────────────────────────────────────────────────────

# Střed okruhu – Klánovice (Praha 21). Souřadnice klidně doluď.
CENTER_LAT = 50.0858
CENTER_LON = 14.6675
RADIUS_KM  = 5.0          # poloměr v km

# Filtr plochy posílaný rovnou do API (zúží objem stahovaných dat).
AREA_FROM = 15            # m²  (širší rozpětí kolem cílových ~50 m²)
AREA_TO   = 90            # m²

# Vyhledávací dotazy na Sreality API.
# NEJSPOLEHLIVĚJŠÍ ZPŮSOB, jak je získat (viz README, sekce „Jak získat dotazy"):
#   1) Na sreality.cz nastav Komerční -> Prodej, na mapě nakresli okruh kolem
#      Klánovic, nastav plochu.
#   2) Otevři DevTools (F12) -> záložka Network -> do filtru napiš: estates
#      -> klikni na request `estates?...` -> zkopíruj jen část za otazníkem.
#   3) Vlož sem jako jeden řádek. Zopakuj pro Pronájem.
#
# Níže je rozumný výchozí dotaz BEZ geografického omezení – funguje hned,
# jen stáhne víc dat (okruh ohlídá haversine filtr níže). Až si zkopíruješ
# vlastní řetězce s nakresleným okruhem, tyhle nahraď.
SEARCH_QUERIES = [
    # Prodej komerčních, plocha {AREA_FROM}-{AREA_TO} m²
    "category_main_cb=4&category_type_cb=1",
    # Pronájem komerčních, plocha {AREA_FROM}-{AREA_TO} m²
    "category_main_cb=4&category_type_cb=2",
]

# Pojistka: i kdyby dotaz nebyl geograficky omezený, necháme jen nabídky
# do RADIUS_KM od středu. Pokud máš v dotazech nakreslený okruh, tahle
# kontrola je neškodná (nic navíc neodfiltruje).
ENFORCE_RADIUS = True

# Technické
API_URL    = "https://www.sreality.cz/api/cs/v2/estates"
PER_PAGE   = 60
MAX_PAGES  = 40          # bezpečnostní strop (60*40 = 2400 nabídek na dotaz)
REQ_DELAY  = 0.7         # pauza mezi requesty (slušnost k API), v sekundách
TIMEOUT    = 20
HEADERS    = {
    "User-Agent": "Mozilla/5.0 (osobni-hlidac-komercnich-prostor)",
    "Accept": "application/json",
}

# Cesty
ROOT        = pathlib.Path(__file__).resolve().parent
STATE_PATH  = ROOT / "state" / "seen.json"
LOG_PATH    = ROOT / "state" / "log.json"
OUTPUT_HTML = ROOT / "docs" / "index.html"
HISTORY_DAYS = 21        # kolik dní novinek držet v archivu na stránce

TZ = datetime.timezone(datetime.timedelta(hours=1))  # jen pro datum/čas v hlavičce


# ──────────────────────────────────────────────────────────────────────────
#  POMOCNÉ FUNKCE
# ──────────────────────────────────────────────────────────────────────────

def haversine_km(lat1, lon1, lat2, lon2):
    """Vzdálenost dvou GPS bodů v km."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


TYPE_SLUG = {1: "prodej", 2: "pronajem", 3: "drazba"}
MAIN_SLUG = {1: "byt", 2: "dum", 3: "pozemek", 4: "komercni", 5: "ostatni"}


def detail_url(estate):
    """Sestaví odkaz na detail. Sreality přesměruje i podle samotného hash_id."""
    hid = estate.get("hash_id")
    seo = estate.get("seo", {}) or {}
    loc = seo.get("locality", "x")
    t = TYPE_SLUG.get(estate.get("seo", {}).get("category_type_cb") or _q_type(estate), "x")
    m = MAIN_SLUG.get(seo.get("category_main_cb", 4), "komercni")
    s = "x"
    return f"https://www.sreality.cz/detail/{t}/{m}/{s}/{loc}/{hid}"


def _q_type(estate):
    return estate.get("_query_type")


def first_image(estate):
    try:
        imgs = estate.get("_links", {}).get("images", [])
        if imgs:
            href = imgs[0].get("href", "")
            return href
    except Exception:
        pass
    return ""


def fmt_price(estate):
    p = estate.get("price")
    if not p or p <= 1:
        return "Cena na vyžádání"
    return f"{p:,.0f} Kč".replace(",", " ")


def area_from_name(name):
    m = re.search(r"(\d{1,4})\s*m²", name or "")
    return f"{m.group(1)} m²" if m else ""


def gps_of(estate):
    g = estate.get("gps", {}) or {}
    lat = g.get("lat")
    lon = g.get("lon")
    return lat, lon


# ──────────────────────────────────────────────────────────────────────────
#  STAŽENÍ DAT
# ──────────────────────────────────────────────────────────────────────────

def fetch_query(query_str, type_id):
    """Stáhne všechny nabídky pro jeden dotaz (paginace)."""
    out = []
    base = dict(urllib.parse.parse_qsl(query_str))
    base.setdefault("usable_area_from", str(AREA_FROM))
    base.setdefault("usable_area_to", str(AREA_TO))
    base["per_page"] = str(PER_PAGE)

    session = requests.Session()
    session.headers.update(HEADERS)

    for page in range(1, MAX_PAGES + 1):
        params = dict(base)
        params["page"] = str(page)
        try:
            r = session.get(API_URL, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  ! chyba při stahování (page {page}): {e}")
            break

        estates = (data.get("_embedded", {}) or {}).get("estates", []) or []
        if not estates:
            break
        for e in estates:
            e["_query_type"] = type_id
            out.append(e)

        total = data.get("result_size", 0)
        if page * PER_PAGE >= total:
            break
        time.sleep(REQ_DELAY)

    return out


def gather():
    """Stáhne a normalizuje všechny nabídky v okruhu."""
    seen_ids = set()
    records = {}

    for q in SEARCH_QUERIES:
        type_id = 2 if "category_type_cb=2" in q else 1
        kind = "pronájem" if type_id == 2 else "prodej"
        print(f"Stahuji dotaz ({kind}): {q}")
        estates = fetch_query(q, type_id)
        print(f"  -> {len(estates)} nabídek z API")

        for e in estates:
            hid = e.get("hash_id")
            if hid is None or hid in seen_ids:
                continue

            lat, lon = gps_of(e)
            if ENFORCE_RADIUS and lat is not None and lon is not None:
                if haversine_km(CENTER_LAT, CENTER_LON, lat, lon) > RADIUS_KM:
                    continue

            seen_ids.add(hid)
            name = e.get("name", "Bez názvu")
            records[str(hid)] = {
                "id": str(hid),
                "name": name,
                "locality": e.get("locality", ""),
                "price": fmt_price(e),
                "area": area_from_name(name),
                "kind": kind,
                "url": detail_url(e),
                "image": first_image(e),
                "lat": lat,
                "lon": lon,
            }

    print(f"V okruhu {RADIUS_KM} km: {len(records)} nabídek")
    return records


# ──────────────────────────────────────────────────────────────────────────
#  STAV + DIFF
# ──────────────────────────────────────────────────────────────────────────

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def run():
    today = datetime.datetime.now(TZ).strftime("%Y-%m-%d")
    current = gather()

    seen = load_json(STATE_PATH, {})          # id -> {first_seen, ...record}
    log = load_json(LOG_PATH, [])             # [{date, ids:[...]}]

    new_records = []
    for hid, rec in current.items():
        if hid not in seen:
            rec["first_seen"] = today
            seen[hid] = rec
            new_records.append(rec)

    # zaznamenat dnešní novinky do logu
    if new_records:
        log = [d for d in log if d.get("date") != today]  # přepiš dnešní běh
        log.insert(0, {"date": today, "ids": [r["id"] for r in new_records]})
        log = log[:HISTORY_DAYS]

    save_json(STATE_PATH, seen)
    save_json(LOG_PATH, log)

    render_html(seen, log, new_records, today)
    print(f"Hotovo. Novinek dnes: {len(new_records)}")


# ──────────────────────────────────────────────────────────────────────────
#  HTML DASHBOARD
# ──────────────────────────────────────────────────────────────────────────

def card(rec, is_new=False):
    img = rec.get("image", "")
    img_html = (
        f'<div class="thumb" style="background-image:url(&quot;{html.escape(img)}&quot;)"></div>'
        if img else '<div class="thumb thumb--empty">bez fotky</div>'
    )
    badge = '<span class="badge">NOVÉ</span>' if is_new else ""
    meta = " · ".join(x for x in [rec.get("area", ""), rec.get("kind", "")] if x)
    return f"""
    <a class="card" href="{html.escape(rec['url'])}" target="_blank" rel="noopener">
      {img_html}
      <div class="card-body">
        <div class="card-top">{badge}<span class="price">{html.escape(rec['price'])}</span></div>
        <h3>{html.escape(rec['name'])}</h3>
        <p class="loc">{html.escape(rec.get('locality',''))}</p>
        <p class="meta">{html.escape(meta)}</p>
      </div>
    </a>"""


def render_html(seen, log, new_records, today):
    new_ids = {r["id"] for r in new_records}

    # Dnešní novinky
    if new_records:
        today_block = '<div class="grid">' + "".join(card(r, True) for r in new_records) + "</div>"
    else:
        today_block = '<p class="empty">Dnes žádná nová nabídka. Hlídám dál.</p>'

    # Archiv předchozích dní
    archive_html = ""
    for day in log:
        if day["date"] == today and new_records:
            continue  # dnešek je nahoře
        recs = [seen[i] for i in day["ids"] if i in seen]
        if not recs:
            continue
        cards = "".join(card(r, False) for r in recs)
        archive_html += f"""
        <details class="day">
          <summary>{html.escape(day['date'])} — {len(recs)} nových</summary>
          <div class="grid">{cards}</div>
        </details>"""

    if not archive_html:
        archive_html = '<p class="empty">Zatím žádná historie.</p>'

    total = len(seen)
    now = datetime.datetime.now(TZ).strftime("%d.%m.%Y %H:%M")

    doc = f"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="3600">
<title>Hlídač komerčních prostor — Klánovice +5 km</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #0f1115;
    --panel: #171a21;
    --panel-2: #1d212a;
    --line: #2a2f3a;
    --ink: #e7e9ee;
    --muted: #8b93a3;
    --accent: #e8c170;
    --new: #7ee0a0;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--ink);
    font-family: "IBM Plex Sans", system-ui, sans-serif;
    background-image: radial-gradient(circle at 20% -10%, #1a2330 0%, transparent 45%),
                      radial-gradient(circle at 100% 0%, #1d2018 0%, transparent 40%);
    min-height: 100vh;
  }}
  .wrap {{ max-width: 1080px; margin: 0 auto; padding: 40px 22px 80px; }}
  header {{ border-bottom: 1px solid var(--line); padding-bottom: 22px; margin-bottom: 34px; }}
  .kicker {{ font-family: "IBM Plex Mono", monospace; font-size: 12px; letter-spacing: .18em;
            text-transform: uppercase; color: var(--accent); margin: 0 0 10px; }}
  h1 {{ font-family: "Fraunces", serif; font-weight: 600; font-size: clamp(28px, 5vw, 44px);
        line-height: 1.05; margin: 0 0 14px; }}
  .status {{ font-family: "IBM Plex Mono", monospace; font-size: 13px; color: var(--muted);
            display: flex; gap: 20px; flex-wrap: wrap; }}
  .status b {{ color: var(--ink); font-weight: 500; }}
  h2 {{ font-family: "Fraunces", serif; font-weight: 500; font-size: 22px;
        margin: 44px 0 18px; display: flex; align-items: baseline; gap: 12px; }}
  h2 .count {{ font-family: "IBM Plex Mono", monospace; font-size: 13px; color: var(--muted); }}
  .grid {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); }}
  .card {{ display: flex; flex-direction: column; background: var(--panel);
          border: 1px solid var(--line); border-radius: 12px; overflow: hidden;
          text-decoration: none; color: inherit; transition: border-color .15s, transform .15s; }}
  .card:hover {{ border-color: var(--accent); transform: translateY(-2px); }}
  .thumb {{ aspect-ratio: 16/10; background-size: cover; background-position: center;
           background-color: var(--panel-2); }}
  .thumb--empty {{ display: flex; align-items: center; justify-content: center;
                  color: var(--muted); font-family: "IBM Plex Mono", monospace; font-size: 12px; }}
  .card-body {{ padding: 14px 15px 16px; display: flex; flex-direction: column; gap: 6px; }}
  .card-top {{ display: flex; align-items: center; gap: 10px; }}
  .price {{ font-family: "IBM Plex Mono", monospace; font-weight: 500; color: var(--accent);
           font-size: 15px; margin-left: auto; }}
  .badge {{ font-family: "IBM Plex Mono", monospace; font-size: 10px; letter-spacing: .1em;
           color: var(--bg); background: var(--new); padding: 2px 7px; border-radius: 4px; font-weight: 500; }}
  .card h3 {{ font-size: 15px; font-weight: 600; margin: 2px 0 0; line-height: 1.25; }}
  .loc {{ color: var(--muted); font-size: 13px; margin: 0; }}
  .meta {{ font-family: "IBM Plex Mono", monospace; color: var(--muted); font-size: 12px; margin: 2px 0 0; }}
  .empty {{ color: var(--muted); font-style: italic; padding: 18px 0; }}
  details.day {{ border: 1px solid var(--line); border-radius: 10px; margin-bottom: 10px;
                background: rgba(255,255,255,.012); }}
  details.day summary {{ cursor: pointer; padding: 13px 16px; font-family: "IBM Plex Mono", monospace;
                        font-size: 13px; color: var(--ink); list-style: none; }}
  details.day summary::-webkit-details-marker {{ display: none; }}
  details.day summary:before {{ content: "▸ "; color: var(--accent); }}
  details.day[open] summary:before {{ content: "▾ "; }}
  details.day .grid {{ padding: 4px 16px 18px; }}
  footer {{ margin-top: 60px; padding-top: 22px; border-top: 1px solid var(--line);
           color: var(--muted); font-family: "IBM Plex Mono", monospace; font-size: 12px; }}
  a.src {{ color: var(--accent); }}
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <p class="kicker">Hlídač komerčních prostor</p>
      <h1>Klánovice a okolí do {RADIUS_KM:g} km</h1>
      <div class="status">
        <span>Poslední běh: <b>{now}</b></span>
        <span>Novinek dnes: <b style="color:var(--new)">{len(new_records)}</b></span>
        <span>Sledováno celkem: <b>{total}</b></span>
      </div>
    </header>

    <section>
      <h2>Dnešní novinky <span class="count">{today}</span></h2>
      {today_block}
    </section>

    <section>
      <h2>Archiv <span class="count">posledních {HISTORY_DAYS} dní</span></h2>
      {archive_html}
    </section>

    <footer>
      Data: Sreality.cz · automaticky aktualizováno přes GitHub Actions ·
      filtr: komerční, plocha {AREA_FROM}–{AREA_TO} m², okruh {RADIUS_KM:g} km od Klánovic
    </footer>
  </div>
</body>
</html>"""

    OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_HTML.write_text(doc, encoding="utf-8")


if __name__ == "__main__":
    run()
