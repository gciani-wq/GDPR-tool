# -*- coding: utf-8 -*-
"""
notify_slack.py — Notificatore Slack (Block Kit) del monitor GDPR Complaion.

Legge /tmp/last_scan_changes.json (prodotto da scrape_gdpr.py) e invia una
notifica formattata sul canale configurato tramite Incoming Webhook.

Convenzioni Complaion:
  - payload Block Kit (header + context + section per evento), non solo testo
  - max 20 eventi inline; oltre, un context block "+ N altre variazioni"
  - summary troncato a 400 caratteri
  - eventuali errori di scraping mostrati in un blocco di ATTENZIONE ben visibile

Il webhook URL è un SEGRETO: arriva SOLO da variabile d'ambiente
SLACK_WEBHOOK_URL (GitHub Secret), mai hard-coded.
"""

import json
import os
import sys

import requests

CHANGES_PATH = os.environ.get("NEW_CHANGES_FILE", "/tmp/last_scan_changes.json")
MAX_INLINE_EVENTS = 20
MAX_SUMMARY_CHARS = 400
SLACK_TIMEOUT = 20

RELEVANCE_EMOJI = {"high": "🔴", "medium": "🟡", "low": "⚪"}

# Notifica anche quando non ci sono variazioni? Default: no (evita rumore giornaliero)
NOTIFY_ON_NO_CHANGES = os.environ.get("SLACK_NOTIFY_ON_NO_CHANGES", "false").lower() == "true"


def load_changes():
    if not os.path.exists(CHANGES_PATH):
        print(f"[notify] nessun file {CHANGES_PATH}: niente da inviare.")
        return None
    with open(CHANGES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def truncate(text, limit=MAX_SUMMARY_CHARS):
    text = (text or "").strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def event_section(ev):
    """Costruisce un blocco 'section' per un singolo evento."""
    emoji = RELEVANCE_EMOJI.get(ev.get("relevance", "medium"), "🟡")
    title = ev.get("title", "(senza titolo)")
    url = ev.get("url", "")
    source = ev.get("source_label", "")
    category = ev.get("category", "Altro")
    date = ev.get("date", "")
    change = ev.get("change_type", "new")
    change_label = {"new": "NUOVO", "changed": "AGGIORNATO"}.get(change, change.upper())
    summary = truncate(ev.get("summary", ""))

    title_line = f"*<{url}|{title}>*" if url else f"*{title}*"
    meta = f"{emoji} `{change_label}` · {source} · _{category}_"
    if date:
        meta += f" · {date}"

    lines = [title_line, meta]
    if summary:
        lines.append(summary)

    key_points = ev.get("key_points", []) or []
    if key_points:
        lines.append("\n".join(f"• {kp}" for kp in key_points[:5]))

    text = "\n".join(lines)
    # limite Slack per section text = 3000 caratteri
    if len(text) > 2900:
        text = text[:2899] + "…"

    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def build_blocks(changes):
    counts = changes.get("counts", {})
    events = changes.get("events", [])
    errors = changes.get("errors", [])
    is_baseline = changes.get("is_baseline", False)
    run_utc = changes.get("run_utc", "")

    n_new = counts.get("new", 0)
    n_changed = counts.get("changed", 0)

    header_text = "🛡️ Monitor GDPR Complaion"
    if is_baseline:
        header_text = "🛡️ Monitor GDPR Complaion — primo scan (baseline)"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text[:150]}},
    ]

    summary_line = f"*{n_new}* nuovi · *{n_changed}* aggiornati"
    if errors:
        summary_line += f" · *{len(errors)}* fonti in errore ⚠️"
    context_elements = [
        {"type": "mrkdwn", "text": summary_line},
        {"type": "mrkdwn", "text": f"scan: {run_utc}"},
    ]
    blocks.append({"type": "context", "elements": context_elements})

    if is_baseline:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": ("Primo avvio: gli item attuali delle fonti sono elencati "
                         "come _nuovi_ per creare la baseline. Dal prossimo run "
                         "verranno segnalate solo le variazioni reali."),
            }],
        })

    # Blocco errori (fail loud) in cima ai contenuti
    if errors:
        err_lines = ["*⚠️ Attenzione: alcune fonti non sono state lette*"]
        for e in errors[:10]:
            err_lines.append(f"• *{e.get('source_label', e.get('source_id'))}*: "
                             f"{truncate(e.get('error', ''), 200)}")
        err_lines.append("_Controllare i log della GitHub Action: possibile blocco "
                         "anti-bot o cambio struttura della fonte._")
        blocks.append({"type": "divider"})
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": "\n".join(err_lines)}})

    if events:
        blocks.append({"type": "divider"})
        for ev in events[:MAX_INLINE_EVENTS]:
            blocks.append(event_section(ev))

        overflow = len(events) - MAX_INLINE_EVENTS
        if overflow > 0:
            blocks.append({
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": f"+ altre *{overflow}* variazioni — vedi il repository "
                            f"`complaion-gdpr-monitor` (docs/data/last_scan.json).",
                }],
            })
    elif not errors:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn",
                                "text": "Nessuna variazione rilevata in questo scan. ✅"}})

    return blocks


def send(blocks, fallback_text):
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        print("[notify] SLACK_WEBHOOK_URL non impostata: impossibile inviare.",
              file=sys.stderr)
        sys.exit(1)

    payload = {"text": fallback_text, "blocks": blocks}
    resp = requests.post(webhook, json=payload, timeout=SLACK_TIMEOUT)
    if resp.status_code != 200 or resp.text.strip() != "ok":
        print(f"[notify] Slack ha risposto {resp.status_code}: {resp.text}",
              file=sys.stderr)
        sys.exit(1)
    print("[notify] notifica Slack inviata con successo.")


def main():
    changes = load_changes()
    if changes is None:
        return

    events = changes.get("events", [])
    errors = changes.get("errors", [])
    is_baseline = changes.get("is_baseline", False)

    if not events and not errors and not is_baseline and not NOTIFY_ON_NO_CHANGES:
        print("[notify] nessuna variazione e nessun errore: notifica saltata.")
        return

    counts = changes.get("counts", {})
    fallback = (f"Monitor GDPR: {counts.get('new', 0)} nuovi, "
                f"{counts.get('changed', 0)} aggiornati, {len(errors)} errori.")
    blocks = build_blocks(changes)

    # Slack ammette max 50 blocchi per messaggio
    if len(blocks) > 50:
        blocks = blocks[:49] + [{
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "… messaggio troncato (limite Slack)."}],
        }]

    send(blocks, fallback)


if __name__ == "__main__":
    main()
