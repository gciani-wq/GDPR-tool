# -*- coding: utf-8 -*-
"""
scrape_gdpr.py — Monitor multi-fonte GDPR/RGPD di Complaion.

Fonti monitorate:
  1. AEPD (Spagna)              — feed RSS
  2. Garante Privacy (Italia)  — feed RSS comunicati stampa
  3. Garante Privacy (Italia)  — feed RSS newsletter
  4. EDPB (UE)                 — RSS autodiscovery + fallback HTML
  5. EUR-Lex                   — sentinella emendamenti al Reg. (UE) 2016/679

Output prodotti (in docs/data/, versionati nel repo = memoria del monitor):
  - last_scan.json      stato/diff arricchito (interno)
  - dashboard.json      dati appiattiti per la dashboard GitHub Pages
  - history.json        serie storica giornaliera (grafico trend 30 gg)
E inoltre:
  - /tmp/last_scan_changes.json   input per notify_slack.py

Comportamento sugli errori: NON fallisce in modo silenzioso. Gli errori per
fonte vengono raccolti, inclusi negli output (Slack + dashboard) e, se ce n'è
almeno uno, lo script esce con codice 1 DOPO aver scritto gli output, così
l'Action risulta rossa su GitHub.
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import feedparser
import requests
from bs4 import BeautifulSoup

import gemini_client

# --------------------------------------------------------------------------- #
# Percorsi e costanti
# --------------------------------------------------------------------------- #
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(REPO_ROOT, "docs", "data")
STATE_PATH = os.path.join(DATA_DIR, "last_scan.json")
DASHBOARD_PATH = os.path.join(DATA_DIR, "dashboard.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
CHANGES_PATH = os.environ.get("NEW_CHANGES_FILE", "/tmp/last_scan_changes.json")

STATE_VERSION = 3

HTTP_HEADERS = {
    "User-Agent": (
        "ComplaionGDPRMonitor/1.0 (+https://complaion.com; compliance monitoring bot)"
    ),
    "Accept-Language": "it,es;q=0.8,en;q=0.6",
}
HTTP_TIMEOUT = 30

MAX_KEYS_PER_SOURCE = 400          # tetto chiavi storicizzate per fonte
SEED_MAX_SUMMARIES_PER_SOURCE = 5  # riassunti AI al primo run (baseline)
MAX_GEMINI_CALLS = 40              # tetto globale chiamate Gemini per run
GEMINI_SLEEP = 1.0                 # pausa tra chiamate Gemini (anti rate-limit)
HISTORY_MAX_DAYS = 120             # entry conservate nella serie storica

# --------------------------------------------------------------------------- #
# Registro fonti
# --------------------------------------------------------------------------- #
SOURCES = [
    {
        "id": "aepd",
        "label": "AEPD (Spagna)",
        "kind": "rss",
        "url": "https://www.aepd.es/noticias/feed.xml",
    },
    {
        "id": "garante_comunicati",
        "label": "Garante Privacy — Comunicati stampa (Italia)",
        "kind": "rss",
        "url": "https://www.garanteprivacy.it/o/gpdp-rss/rss?c=10490",
    },
    {
        "id": "garante_newsletter",
        "label": "Garante Privacy — Newsletter (Italia)",
        "kind": "rss",
        "url": "https://www.garanteprivacy.it/o/gpdp-rss/rss?c=10524",
    },
    {
        "id": "edpb",
        "label": "EDPB (UE)",
        "kind": "rss",
        # Feed RSS diretto: la pagina HTML news_en è renderizzata via JS e non
        # espone i link nel sorgente statico, quindi si usa il feed.
        "url": "https://www.edpb.europa.eu/feed/news_en",
    },
    # EUR-Lex disattivato: pagina versioni consolidate caricata via JS,
    # scraping statico non affidabile. Da riattivare con metodo dedicato.
]


# --------------------------------------------------------------------------- #
# Utility
# --------------------------------------------------------------------------- #
def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def http_get(url, retries=2, backoff=3):
    """GET con User-Agent, timeout e retry. Solleva l'ultima eccezione."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_err = exc
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise last_err


def content_hash(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8", errors="ignore"))
    return h.hexdigest()[:16]


def to_ts(value):
    """Best-effort: struct_time -> timestamp epoch (0 se non parsabile)."""
    if value is None:
        return 0
    try:
        if hasattr(value, "tm_year"):
            return int(time.mktime(value))
    except Exception:
        pass
    return 0


# --------------------------------------------------------------------------- #
# Parser per tipologia di fonte  (validati con test offline)
# --------------------------------------------------------------------------- #
def _items_from_feed(content, source_id):
    """Normalizza gli entry di un feed RSS/Atom già scaricato."""
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"feed non parsabile: {getattr(feed, 'bozo_exception', '?')}")

    items = []
    for entry in feed.entries:
        link = entry.get("link", "").strip()
        guid = entry.get("id", "") or link
        title = (entry.get("title", "") or "").strip()
        raw_html = entry.get("summary", "") or ""
        raw_text = BeautifulSoup(raw_html, "lxml").get_text(" ", strip=True) if raw_html else ""
        published = entry.get("published", "") or entry.get("updated", "")
        ts = to_ts(entry.get("published_parsed") or entry.get("updated_parsed"))

        if not (link or guid):
            continue
        key = f"{source_id}:{guid or link}"
        items.append({
            "key": key,
            "title": title,
            "url": link,
            "date": published,
            "raw_text": raw_text[:1500],
            "ts": ts,
            "hash": content_hash(title, published),
        })
    return items


def parse_rss(source):
    """Parsifica un feed RSS/Atom generico."""
    resp = http_get(source["url"])
    return _items_from_feed(resp.content, source["id"])


EDPB_BASE = "https://www.edpb.europa.eu"
_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}
_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{4})\b")


def _edpb_scoped_container(anchor):
    """Trova il contenitore più ampio che contiene SOLO questo anchor news."""
    best = anchor
    node = anchor
    for _ in range(6):
        parent = node.parent
        if parent is None:
            break
        news_links = [a for a in parent.find_all("a", href=True)
                      if "/news/" in a["href"]]
        if len(news_links) > 1:
            break
        best = parent
        node = parent
    return best


def _parse_edpb_html(content, source_id):
    """Fallback: parsifica il listing news EDPB scopando ogni singolo item."""
    soup = BeautifulSoup(content, "lxml")
    items = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "/news/" not in href:
            continue
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 8:
            continue
        if href.startswith("/"):
            url = EDPB_BASE + href
        elif href.startswith("http"):
            url = href
        else:
            continue
        if url in seen:
            continue
        seen.add(url)

        container = _edpb_scoped_container(a)
        ctx_text = container.get_text(" ", strip=True) if container else ""
        m = _DATE_RE.search(ctx_text)
        date = m.group(0) if m else ""
        ts = 0
        if m:
            try:
                ts = int(datetime(int(m.group(3)), _MONTHS[m.group(2)],
                                  int(m.group(1)), tzinfo=timezone.utc).timestamp())
            except Exception:
                ts = 0
        badge = ""
        for label in ("One-Stop-Shop News", "National News", "EDPB News"):
            if label in ctx_text:
                badge = label
                break

        items.append({
            "key": f"{source_id}:{url}",
            "title": title,
            "url": url,
            "date": date,
            "raw_text": "",
            "extra": f"Tipologia EDPB: {badge}" if badge else "",
            "ts": ts,
            "hash": content_hash(title, date),
        })

    if not items:
        raise RuntimeError(
            "nessuna news EDPB estratta: possibile blocco anti-bot o cambio struttura pagina")
    return items


def parse_edpb(source):
    """EDPB: prova prima il feed RSS di autodiscovery, poi il fallback HTML."""
    resp = http_get(source["url"])
    soup = BeautifulSoup(resp.content, "lxml")

    feed_link = soup.find("link", attrs={"type": "application/rss+xml"})
    feed_href = feed_link.get("href") if feed_link else None
    if feed_href:
        if feed_href.startswith("/"):
            feed_href = EDPB_BASE + feed_href
        try:
            feed_resp = http_get(feed_href)
            items = _items_from_feed(feed_resp.content, source["id"])
            if items:
                return items
        except Exception as exc:
            print(f"[EDPB] feed RSS {feed_href} non utilizzabile ({exc}); "
                  f"fallback su HTML.", file=sys.stderr)

    return _parse_edpb_html(resp.content, source["id"])


def parse_eurlex(source):
    """Sentinella: individua la data dell'ultima versione consolidata del GDPR."""
    resp = http_get(source["url"])
    html = resp.text
    dates = sorted(set(re.findall(r"02016R0679-(\d{8})", html)))
    if not dates:
        raise RuntimeError(
            "impossibile individuare la versione consolidata GDPR su EUR-Lex "
            "(nessun CELEX 02016R0679-YYYYMMDD): possibile blocco anti-bot o "
            "cambio struttura pagina")
    latest = dates[-1]
    amendments = sorted(set(re.findall(r"CELEX:(3\d{4}[A-Z]\d{4})", html)))
    pretty = f"{latest[6:8]}/{latest[4:6]}/{latest[0:4]}"
    return [{
        "key": "eurlex:02016R0679",
        "title": f"GDPR — versione consolidata al {pretty}",
        "url": source["url"],
        "date": pretty,
        "raw_text": "",
        "extra": (f"Ultima versione consolidata: {pretty}. "
                  f"Atti collegati/modificativi rilevati: {len(amendments)}."),
        "ts": 0,
        "hash": content_hash(latest, ",".join(amendments)),
        "consolidated_version": latest,
        "amendments": amendments,
    }]


PARSERS = {
    "rss": parse_rss,
    "edpb_html": parse_edpb,
    "eurlex": parse_eurlex,
}


# --------------------------------------------------------------------------- #
# Stato e diff
# --------------------------------------------------------------------------- #
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def diff_source(source, current_items, stored_items, today):
    """Confronta gli item correnti con lo stato storicizzato (arricchito).

    'removed' non è tracciato per le fonti news. Preserva l'arricchimento AI
    (categoria/rilevanza/riassunto) degli item invariati.
    """
    events = []
    stored = dict(stored_items or {})
    sid = source["id"]
    label = source["label"]

    for item in current_items:
        key = item["key"]
        prev = stored.get(key)
        if prev is None:
            status, change_type = "new", "new"
            first_seen, last_change = today, today
        elif prev.get("hash") != item.get("hash"):
            status, change_type = "changed", "changed"
            first_seen = prev.get("first_seen", today)
            last_change = today
        else:
            status, change_type = "seen", None
            first_seen = prev.get("first_seen", today)
            last_change = prev.get("last_change", first_seen)

        if change_type:
            events.append({**item, "change_type": change_type,
                           "source_id": sid, "source_label": label})

        entry = {
            "title": item["title"],
            "url": item["url"],
            "date": item["date"],
            "hash": item["hash"],
            "ts": item.get("ts", 0),
            "source_id": sid,
            "source_label": label,
            "status": status,
            "first_seen": first_seen,
            "last_change": last_change,
            # arricchimento: conserva quello precedente per gli invariati,
            # verrà (ri)compilato dopo Gemini per new/changed
            "category": prev.get("category", "") if (prev and not change_type) else "",
            "relevance": prev.get("relevance", "") if (prev and not change_type) else "",
            "summary": prev.get("summary", "") if (prev and not change_type) else "",
        }
        if "consolidated_version" in item:
            entry["consolidated_version"] = item["consolidated_version"]
        stored[key] = entry

    if len(stored) > MAX_KEYS_PER_SOURCE:
        ordered = sorted(stored.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)
        stored = dict(ordered[:MAX_KEYS_PER_SOURCE])

    return events, stored


# --------------------------------------------------------------------------- #
# Costruzione output dashboard
# --------------------------------------------------------------------------- #
def _within_days(date_str, days, today_dt):
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return (today_dt - d).days <= days
    except Exception:
        return False


def update_history(history, today, counts):
    """Aggiorna la serie storica giornaliera (una entry per giorno)."""
    history = [h for h in (history or []) if isinstance(h, dict) and h.get("date")]
    by_date = {h["date"]: h for h in history}
    entry = by_date.get(today, {"date": today, "new": 0, "changed": 0})
    entry["new"] = counts.get("new", 0)
    entry["changed"] = counts.get("changed", 0)
    by_date[today] = entry
    ordered = sorted(by_date.values(), key=lambda h: h["date"])
    return ordered[-HISTORY_MAX_DAYS:]


def build_dashboard(state, history, errors, run_utc):
    """Appiattisce lo stato in un JSON ottimizzato per la dashboard."""
    today_dt = datetime.strptime(today_str(), "%Y-%m-%d")
    items = []
    by_source = {}
    by_relevance = {"high": 0, "medium": 0, "low": 0}
    changes_7d = changes_30d = 0

    for sid, sdata in state.get("sources", {}).items():
        for key, it in sdata.get("items", {}).items():
            items.append({
                "id": key,
                "source_id": it.get("source_id", sid),
                "source_label": it.get("source_label", sid),
                "title": it.get("title", ""),
                "url": it.get("url", ""),
                "date": it.get("date", ""),
                "status": it.get("status", "seen"),
                "category": it.get("category", ""),
                "relevance": it.get("relevance", ""),
                "summary": it.get("summary", ""),
                "first_seen": it.get("first_seen", ""),
                "last_change": it.get("last_change", ""),
                "ts": it.get("ts", 0),
            })
            by_source[it.get("source_label", sid)] = by_source.get(it.get("source_label", sid), 0) + 1
            rel = it.get("relevance", "")
            if rel in by_relevance:
                by_relevance[rel] += 1
            if _within_days(it.get("last_change", ""), 7, today_dt):
                changes_7d += 1
            if _within_days(it.get("last_change", ""), 30, today_dt):
                changes_30d += 1

    # ordina per data di ultima variazione / recency
    items.sort(key=lambda x: (x.get("last_change", ""), x.get("ts", 0)), reverse=True)

    return {
        "generated_utc": run_utc,
        "stats": {
            "total_items": len(items),
            "changes_7d": changes_7d,
            "changes_30d": changes_30d,
            "sources_count": len(SOURCES),
            "by_source": by_source,
            "by_relevance": by_relevance,
        },
        "timeline": history,
        "items": items,
        "errors": [{"source_label": e.get("source_label"), "error": e.get("error")}
                   for e in errors],
    }


# --------------------------------------------------------------------------- #
# Orchestrazione
# --------------------------------------------------------------------------- #
def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    prev_state = load_json(STATE_PATH, None)
    history = load_json(HISTORY_PATH, [])
    is_baseline = prev_state is None
    prev_sources = (prev_state or {}).get("sources", {})
    today = today_str()

    new_state = {"version": STATE_VERSION, "last_run_utc": now_utc_iso(), "sources": {}}
    all_events = []
    errors = []
    per_source_seed_count = {}

    for source in SOURCES:
        sid = source["id"]
        parser = PARSERS[source["kind"]]
        stored_items = prev_sources.get(sid, {}).get("items", {})
        try:
            current_items = parser(source)
        except Exception as exc:
            msg = f"{source['label']}: {exc}"
            print(f"[ERRORE] {msg}", file=sys.stderr)
            errors.append({"source_id": sid, "source_label": source["label"],
                           "error": str(exc)})
            new_state["sources"][sid] = {"items": stored_items}  # mantieni stato
            continue

        events, updated_items = diff_source(source, current_items, stored_items, today)
        new_state["sources"][sid] = {"items": updated_items}
        events.sort(key=lambda e: e.get("ts", 0), reverse=True)
        all_events.extend(events)
        per_source_seed_count[sid] = 0
        print(f"[OK] {source['label']}: {len(current_items)} item, {len(events)} variazioni")

    # ------------------------------------------------------------------ #
    # Arricchimento AI (Gemini) — con tetti anti-costo
    # ------------------------------------------------------------------ #
    gemini_calls = 0
    for ev in all_events:
        sid = ev["source_id"]
        skip_ai = False
        if is_baseline and per_source_seed_count.get(sid, 0) >= SEED_MAX_SUMMARIES_PER_SOURCE:
            skip_ai = True
        if gemini_calls >= MAX_GEMINI_CALLS:
            skip_ai = True

        if skip_ai:
            result = {"summary": ev["title"], "category": "Comunicazione / News",
                      "relevance": "low", "key_points": [], "ai_ok": False}
        else:
            result = gemini_client.summarize_event(ev)
            gemini_calls += 1
            per_source_seed_count[sid] = per_source_seed_count.get(sid, 0) + 1
            time.sleep(GEMINI_SLEEP)

        ev.update(result)
        # scrivi l'arricchimento anche nello stato persistente (per la dashboard)
        entry = new_state["sources"][sid]["items"].get(ev["key"])
        if entry is not None:
            entry["category"] = result.get("category", "")
            entry["relevance"] = result.get("relevance", "")
            entry["summary"] = result.get("summary", "")

    # ------------------------------------------------------------------ #
    # Output
    # ------------------------------------------------------------------ #
    counts = {
        "new": sum(1 for e in all_events if e.get("change_type") == "new"),
        "changed": sum(1 for e in all_events if e.get("change_type") == "changed"),
        "removed": 0,
    }
    history = update_history(history, today, counts)

    clean_events = [{
        "source_id": e.get("source_id"),
        "source_label": e.get("source_label"),
        "change_type": e.get("change_type"),
        "title": e.get("title"),
        "url": e.get("url"),
        "date": e.get("date"),
        "summary": e.get("summary", e.get("title")),
        "category": e.get("category", "Altro"),
        "relevance": e.get("relevance", "medium"),
        "key_points": e.get("key_points", []),
        "ai_ok": e.get("ai_ok", False),
    } for e in all_events]

    changes = {
        "run_utc": new_state["last_run_utc"],
        "is_baseline": is_baseline,
        "counts": counts,
        "events": clean_events,
        "errors": errors,
    }

    dashboard = build_dashboard(new_state, history, errors, new_state["last_run_utc"])

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(new_state, f, ensure_ascii=False, indent=2, sort_keys=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        json.dump(dashboard, f, ensure_ascii=False, indent=2)
    with open(CHANGES_PATH, "w", encoding="utf-8") as f:
        json.dump(changes, f, ensure_ascii=False, indent=2)

    print(f"\nRiepilogo: {counts['new']} nuovi, {counts['changed']} cambiati, "
          f"{len(errors)} errori. Baseline={is_baseline}. Gemini={gemini_calls}. "
          f"Item totali in dashboard={dashboard['stats']['total_items']}.")

    # Fail-loud solo se TUTTE le fonti falliscono (outage vero): un errore
    # parziale è visibile su dashboard e Slack, ma non rende rosso il run.
    if errors:
        print(f"[ATTENZIONE] {len(errors)} fonte/i in errore su {len(SOURCES)} "
              f"(vedi banner sulla dashboard).", file=sys.stderr)
    if errors and len(errors) >= len(SOURCES):
        print("[ERRORE] tutte le fonti in errore — l'Action verrà marcata come "
              "fallita.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
