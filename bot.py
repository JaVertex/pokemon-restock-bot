#!/usr/bin/env python3
"""
JaPokeAlertBot - a free Canadian Pokemon TCG restock notifier.

Runs on GitHub Actions every few minutes. Each run it:
  1. Answers your Telegram commands (/latest, /stores, /report ...)
  2. Checks Canadian hobby stores' public product lists for new stock
  3. Checks news feeds for Costco / Walmart / EB Games / Pokemon Center news
  4. Sends alerts to your Telegram and saves its memory to state.json

It only uses public feeds, checks each website's robots.txt rules first,
and never tries to get around CAPTCHAs, queues or bot protection.
Uses only Python's built-in libraries, so nothing needs installing.
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TZ = ZoneInfo("America/Toronto")
USER_AGENT = "JaPokeAlertBot/1.0 (personal Pokemon TCG restock notifier)"
STATE_FILE = "state.json"
CONFIG_FILE = "config.json"

RUMOUR_WORDS = [
    "expected", "rumor", "rumour", "leak", "could ", "may ", "might ",
    "reportedly", "coming soon", "next week", "set to", "will drop",
    "dropping", "possible", "allegedly", "spotted", "upcoming",
]

e = html.escape  # makes text safe to put inside Telegram HTML messages


# ---------------------------------------------------------------- helpers

def now_utc():
    return datetime.now(timezone.utc)


def fmt_time(dt):
    return dt.astimezone(TZ).strftime("%a %b %d, %I:%M %p ET")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


_robots_cache = {}


def allowed_by_robots(url):
    """Returns True only if the site's robots.txt allows fetching this URL."""
    p = urllib.parse.urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            rp.parse(http_get(base + "/robots.txt").decode("utf-8", "replace").splitlines())
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                rp.disallow_all = True      # site refuses even robots.txt: stay out
            else:
                rp.allow_all = True         # no robots.txt file = no rules
        except Exception:
            rp.disallow_all = True          # couldn't check the rules: skip for now
        _robots_cache[base] = rp
    return _robots_cache[base].can_fetch(USER_AGENT, url)


# ---------------------------------------------------------------- telegram

def telegram(method, params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def send(text, chat_id=None):
    """Sends a message, splitting it if it's longer than Telegram allows."""
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) > 3800:
            chunks.append(current)
            current = ""
        current += line + "\n"
    chunks.append(current)
    for chunk in chunks:
        if not chunk.strip():
            continue
        try:
            telegram("sendMessage", {
                "chat_id": chat_id or CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            })
        except Exception as err:
            print("Telegram send failed, retrying as plain text:", err)
            plain = html.unescape(re.sub(r"<[^>]+>", "", chunk))
            try:
                telegram("sendMessage", {"chat_id": chat_id or CHAT_ID, "text": plain,
                                         "disable_web_page_preview": "true"})
            except Exception as err2:
                print("Telegram send failed:", err2)
        time.sleep(0.5)


def maps_link(place):
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(place + ", Ontario")


def add_history(state, kind, label, title, where, link):
    state["history"].append({
        "kind": kind, "label": label, "title": title, "where": where,
        "link": link, "time": now_utc().isoformat(),
    })
    state["history"] = state["history"][-200:]


# ---------------------------------------------------------------- stores

def all_stores(cfg, state):
    stores = [dict(s, custom=False) for s in cfg["stores"]]
    stores += [dict(s, custom=True) for s in state["custom_stores"]]
    return stores


def has_word(text, word):
    """True if `word` appears as a whole word/phrase (so 'tin' doesn't match 'Destined')."""
    return re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", text) is not None


def usual_price(title, cfg):
    t = title.lower()
    for rule in cfg["usual_prices"]:
        if all(has_word(t, k) for k in rule["keywords"]):
            return rule["name"], float(rule["price"])
    return None, None


def is_sealed_pokemon(title, product_type, cfg):
    t = f"{title} {product_type}".lower()
    if "pokemon" not in t and "pokémon" not in t:
        return False
    title_l = title.lower()
    if any(has_word(title_l, x) for x in cfg["exclude_words"]):
        return False
    return any(has_word(title_l, x) for x in cfg["include_words"])


def stock_message(store_name, title, price, link, cfg):
    name, usual = usual_price(title, cfg)
    try:
        p = float(price)
    except (TypeError, ValueError):
        p = None
    price_line = f"Price: <b>${p:.2f} CAD</b>" if p is not None else "Price: unknown"
    if usual and p is not None:
        if p <= usual + 0.01:
            verdict = f"✅ At or below usual retail (est. ${usual:.2f} for {e(name)})"
        else:
            verdict = f"⚠️ Above usual retail (est. ${usual:.2f} for {e(name)})"
    else:
        verdict = "❔ No usual retail price on file for this product type"
    return (
        "🛒 <b>IN STOCK ONLINE — CONFIRMED</b>\n"
        f"<b>{e(title)}</b>\n"
        f"Store: {e(store_name)}\n"
        f"{price_line}\n{verdict}\n"
        f'🔗 <a href="{e(link)}">Buy here</a>\n'
        f"Detected: {fmt_time(now_utc())}\n"
        "<i>Seen on the store's public product list. Hot items can sell out in minutes.</i>"
    )


def fetch_products(base):
    """Tries the store's Pokemon collection first, then its full product list."""
    tried = []
    for path in ("/collections/pokemon/products.json", "/products.json"):
        url = base + path + "?limit=250"
        if not allowed_by_robots(url):
            tried.append("robots")
            continue
        products, ok = [], False
        for page in range(1, 5):
            try:
                data = json.loads(http_get(f"{url}&page={page}"))
            except urllib.error.HTTPError as err:
                tried.append(f"HTTP {err.code}")
                break
            except Exception as err:
                tried.append(type(err).__name__)
                break
            ok = True
            batch = data.get("products", [])
            products += batch
            if len(batch) < 250:
                break
            time.sleep(1)
        if ok and products:
            return products, None
        if ok:
            tried.append("empty")
    if tried and all(t == "robots" for t in tried):
        return None, "skipped: the store's robots.txt rules don't allow it"
    return None, "not supported (" + ", ".join(tried) + ")"


def check_store(store, state, cfg, alerts):
    base = store["url"].rstrip("/")
    st = state["stores"].setdefault(base, {"initialized": False, "variants": {}})
    products, problem = fetch_products(base)
    st["checked"] = now_utc().isoformat()
    if problem:
        st["status"] = problem
        return
    first_time = not st["initialized"]
    watched = 0
    for prod in products:
        title = prod.get("title", "")
        if not is_sealed_pokemon(title, prod.get("product_type", ""), cfg):
            continue
        watched += 1
        link = f"{base}/products/{prod.get('handle', '')}"
        for v in prod.get("variants", []):
            vid = str(v.get("id"))
            available = bool(v.get("available"))
            vtitle = v.get("title") or ""
            full_title = title if vtitle in ("", "Default Title") else f"{title} ({vtitle})"
            before = st["variants"].get(vid)
            st["variants"][vid] = {"a": available, "p": v.get("price")}
            newly_in_stock = available and (before is None or not before["a"])
            if newly_in_stock and not first_time:
                alerts.append({
                    "text": stock_message(store["name"], full_title, v.get("price"), link, cfg),
                    "kind": "stock", "label": "CONFIRMED", "title": full_title,
                    "where": store["name"], "link": link,
                })
    st["initialized"] = True
    st["status"] = f"OK — watching {watched} sealed Pokémon products"


# ---------------------------------------------------------------- news

def google_news_url(query):
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{query} when:7d", "hl": "en-CA", "gl": "CA", "ceid": "CA:en"})


def parse_feed(raw):
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):
        src = it.find("source")
        items.append({
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "date": it.findtext("pubDate") or "",
            "source": (src.text or "").strip() if src is not None else "",
        })
    atom = "{http://www.w3.org/2005/Atom}"
    for en in root.iter(atom + "entry"):
        link_el = en.find(atom + "link")
        items.append({
            "title": (en.findtext(atom + "title") or "").strip(),
            "link": link_el.get("href", "") if link_el is not None else "",
            "date": en.findtext(atom + "updated") or en.findtext(atom + "published") or "",
            "source": "",
        })
    return items


def parse_date(text):
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def classify(title, source, cfg):
    if source.lower() in [s.lower() for s in cfg["official_sources"]]:
        return "CONFIRMED"
    t = f" {title.lower()} "
    if any(w in t for w in RUMOUR_WORDS):
        return "RUMOURED"
    return "REPORTED"


def news_key(title):
    t = re.sub(r"\s+-\s+[^-]+$", "", title.lower())  # drop " - Publisher" ending
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def check_news(state, cfg, alerts):
    feeds = [(f"Google News: {q}", google_news_url(q)) for q in cfg["news_searches"]]
    feeds += [(f["name"], f["url"]) for f in cfg["extra_feeds"]]
    seen = set(state["news_seen"])
    max_age = timedelta(hours=cfg["news_max_age_hours"])
    for name, url in feeds:
        status_key = name
        if not allowed_by_robots(url):
            state["source_status"][status_key] = "skipped: robots.txt doesn't allow it"
            continue
        try:
            items = parse_feed(http_get(url))
        except Exception as err:
            state["source_status"][status_key] = f"error: {type(err).__name__}"
            continue
        first_time = url not in state["news_initialized"]
        for it in items:
            key = news_key(it["title"])
            if not key or key in seen:
                continue
            seen.add(key)
            state["news_seen"].append(key)
            low = it["title"].lower()
            if "pokemon" not in low and "pokémon" not in low:
                continue
            published = parse_date(it["date"])
            if first_time or published is None or now_utc() - published > max_age:
                continue
            label = classify(it["title"], it["source"], cfg)
            icon = {"CONFIRMED": "🟢", "REPORTED": "🟡", "RUMOURED": "🟠"}[label]
            source = it["source"] or name
            note = {
                "CONFIRMED": "From an official source.",
                "REPORTED": "News/community report — not confirmed by the store.",
                "RUMOURED": "⚠️ Unverified — the store has NOT confirmed this.",
            }[label]
            text = (
                f"📰 {icon} <b>{label}</b>\n"
                f"<b>{e(it['title'])}</b>\n"
                f"Source: {e(source)}\n"
                f"Published: {fmt_time(published)}\n"
                f"Detected: {fmt_time(now_utc())}\n"
                f'🔗 <a href="{e(it["link"])}">Read it</a>\n'
                f"<i>{note}</i>"
            )
            alerts.append({"text": text, "kind": "news", "label": label,
                           "title": it["title"], "where": source, "link": it["link"]})
        state["news_initialized"].append(url)
        state["source_status"][status_key] = f"OK — {len(items)} items checked"
        time.sleep(1)
    state["news_initialized"] = sorted(set(state["news_initialized"]))
    state["news_seen"] = state["news_seen"][-2000:]


# ---------------------------------------------------------------- commands

HELP = (
    "🤖 <b>JaPokeAlertBot commands</b>\n\n"
    "/latest — the 10 most recent alerts and reports\n"
    "/stores — online stores being watched, and whether each one works\n"
    "/addstore <i>website</i> — watch another Canadian store\n"
    "   e.g. <code>/addstore https://example.ca</code>\n"
    "/removestore <i>number</i> — stop watching a store (number from /stores)\n"
    "/sources — status of the news feeds\n"
    "/report <i>store | product | price</i> — log an in-store sighting\n"
    "   e.g. <code>/report Costco Vaughan | Ascended Heroes ETB | 79.99</code>\n"
    "/rumour <i>store | details</i> — log an unconfirmed tip\n"
    "   e.g. <code>/rumour Walmart Vaughan Mills | cards expected Friday</code>\n\n"
    "⏱ I check every 5–15 minutes, so replies to commands can take that long too."
)


def cmd_stores(cfg, state):
    lines = ["🏪 <b>Online stores I watch</b>\n"]
    for i, s in enumerate(all_stores(cfg, state), 1):
        base = s["url"].rstrip("/")
        if base in state["disabled"]:
            status = "⏸ removed (use /addstore to bring back)"
        else:
            status = state["stores"].get(base, {}).get("status", "not checked yet")
        lines.append(f"{i}. <b>{e(s['name'])}</b> — {e(status)}")
    lines.append("\n<i>Not checked directly (bot protection): Pokémon Center, Walmart, "
                 "Costco, Best Buy, EB Games, Toys R Us. News about them comes through /sources.</i>")
    return "\n".join(lines)


def cmd_sources(cfg, state):
    lines = ["📰 <b>News sources</b>\n"]
    names = [f"Google News: {q}" for q in cfg["news_searches"]] + [f["name"] for f in cfg["extra_feeds"]]
    for n in names:
        lines.append(f"• {e(n)} — {e(state['source_status'].get(n, 'not checked yet'))}")
    return "\n".join(lines)


def cmd_latest(state):
    items = state["history"][-10:][::-1]
    if not items:
        return "Nothing yet! I'll list alerts and reports here as they come in."
    lines = ["🕑 <b>Latest alerts</b>\n"]
    for h in items:
        when = fmt_time(datetime.fromisoformat(h["time"]))
        link = f' — <a href="{e(h["link"])}">link</a>' if h.get("link") else ""
        lines.append(f"• [{h['label']}] {e(h['title'])} — {e(h['where'])} · {when}{link}")
    return "\n".join(lines)


def cmd_report(arg, label, reporter, state):
    parts = [p.strip() for p in arg.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        example = ("/report Costco Vaughan | Ascended Heroes ETB | 79.99" if label == "REPORTED"
                   else "/rumour Walmart Vaughan Mills | cards expected Friday")
        return None, f"Please use this format:\n<code>{e(example)}</code>"
    store, product = parts[0], parts[1]
    price = parts[2] if len(parts) > 2 and parts[2] else "unknown"
    if label == "REPORTED":
        head = "📍 🟡 <b>REPORTED — in-store sighting</b>"
        stock_line = "Inventory: <b>not confirmed by the store</b> — call ahead before you go."
    else:
        head = "📍 🟠 <b>RUMOURED — unverified tip</b>"
        stock_line = "⚠️ <b>Unverified.</b> The store has NOT confirmed this."
    text = (
        f"{head}\n"
        f"Store: <b>{e(store)}</b>\n"
        f"{'Product' if label == 'REPORTED' else 'Details'}: {e(product)}\n"
        + (f"Price: {e(price)}\n" if label == "REPORTED" else "")
        + f"When: {fmt_time(now_utc())}\n"
        f"Source: Telegram report from {e(reporter)}\n"
        f'🗺 <a href="{e(maps_link(store))}">Google Maps</a> '
        "(exact address not verified — check the pin before you go)\n"
        f"{stock_line}"
    )
    add_history(state, "report", label, product, store, maps_link(store))
    return text, None


def handle_commands(cfg, state):
    try:
        res = telegram("getUpdates", {"offset": state["tg_offset"], "timeout": 0,
                                      "allowed_updates": json.dumps(["message"])})
    except Exception as err:
        print("Couldn't read Telegram commands:", err)
        return
    allowed = {CHAT_ID} | {str(x) for x in cfg.get("extra_reporter_ids", [])}
    for upd in res.get("result", []):
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat = str(msg.get("chat", {}).get("id", ""))
        sender = msg.get("from", {})
        text = (msg.get("text") or "").strip()
        if not text.startswith("/") or chat not in allowed:
            continue  # ignore normal chat and strangers
        is_owner = chat == CHAT_ID
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        reply = None

        if cmd in ("/start", "/help"):
            reply = HELP
        elif cmd in ("/report", "/rumour", "/rumor"):
            label = "REPORTED" if cmd == "/report" else "RUMOURED"
            who = sender.get("first_name") or "a trusted reporter"
            alert, problem = cmd_report(arg, label, who, state)
            if problem:
                reply = problem
            else:
                send(alert)  # always goes to the owner
                if not is_owner:
                    reply = "Thanks! Your report was sent."
        elif not is_owner:
            reply = "Sorry, only /report and /rumour are available to you."
        elif cmd == "/latest":
            reply = cmd_latest(state)
        elif cmd == "/stores":
            reply = cmd_stores(cfg, state)
        elif cmd == "/sources":
            reply = cmd_sources(cfg, state)
        elif cmd == "/addstore":
            if not arg:
                reply = "Please include the store's website, e.g. <code>/addstore https://example.ca</code>"
            else:
                url = arg if arg.startswith("http") else "https://" + arg
                p = urllib.parse.urlparse(url)
                base = f"https://{p.netloc.lower()}"
                if base in state["disabled"]:
                    state["disabled"].remove(base)
                    reply = f"Brought back {e(base)} ✅"
                elif base in [s["url"].rstrip("/") for s in all_stores(cfg, state)]:
                    reply = "I'm already watching that store."
                elif "." not in p.netloc:
                    reply = "That doesn't look like a website address."
                else:
                    name = p.netloc.lower().removeprefix("www.")
                    state["custom_stores"].append({"name": name, "url": base})
                    reply = (f"Added <b>{e(name)}</b> ✅\nI'll try it on my next check. "
                             "Use /stores afterwards to see whether it works.")
        elif cmd == "/removestore":
            stores = all_stores(cfg, state)
            if not arg.isdigit() or not 1 <= int(arg) <= len(stores):
                reply = "Send the store's number from /stores, e.g. <code>/removestore 2</code>"
            else:
                s = stores[int(arg) - 1]
                base = s["url"].rstrip("/")
                if s["custom"]:
                    state["custom_stores"] = [c for c in state["custom_stores"]
                                              if c["url"].rstrip("/") != base]
                elif base not in state["disabled"]:
                    state["disabled"].append(base)
                reply = f"Stopped watching <b>{e(s['name'])}</b>."
        else:
            reply = "I don't know that command. Send /help to see what I can do."

        if reply:
            send(reply, chat_id=chat)


# ---------------------------------------------------------------- main

def main():
    if not TOKEN or not CHAT_ID:
        sys.exit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret.")
    cfg = load_json(CONFIG_FILE, None)
    if cfg is None:
        sys.exit("config.json is missing.")
    state = load_json(STATE_FILE, {})
    defaults = {"stores": {}, "custom_stores": [], "disabled": [], "history": [],
                "news_seen": [], "news_initialized": [], "source_status": {},
                "tg_offset": 0, "last_news_check": None, "welcomed": False}
    for k, v in defaults.items():
        state.setdefault(k, v)

    try:
        if not state["welcomed"]:
            send("✅ <b>JaPokeAlertBot is live!</b>\n\nMy first check just records what's "
                 "already in stock, so you won't get flooded. From now on you'll only hear "
                 "about <b>new</b> stock and news.\n\n" + HELP)
            state["welcomed"] = True

        handle_commands(cfg, state)

        alerts = []
        for store in all_stores(cfg, state):
            if store["url"].rstrip("/") in state["disabled"]:
                continue
            try:
                check_store(store, state, cfg, alerts)
            except Exception as err:
                print(f"Store {store['name']} failed:", err)
            time.sleep(1)

        last = state["last_news_check"]
        due = last is None or now_utc() - datetime.fromisoformat(last) >= timedelta(
            minutes=cfg["news_every_minutes"])
        if due:
            try:
                check_news(state, cfg, alerts)
            except Exception as err:
                print("News check failed:", err)
            state["last_news_check"] = now_utc().isoformat()

        for a in alerts[: cfg["max_alerts_per_run"]]:
            send(a["text"])
            add_history(state, a["kind"], a["label"], a["title"], a["where"], a["link"])
        if len(alerts) > cfg["max_alerts_per_run"]:
            send(f"…plus {len(alerts) - cfg['max_alerts_per_run']} more. Send /latest to see recent ones.")
            for a in alerts[cfg["max_alerts_per_run"]:]:
                add_history(state, a["kind"], a["label"], a["title"], a["where"], a["link"])
        print(f"Done. {len(alerts)} new alert(s).")
    finally:
        save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
