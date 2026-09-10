# -*- coding: utf-8 -*-
"""
gemini_client.py — Riassunto e classificazione AI dei contenuti normativi (Gemini).

Espone una singola funzione `summarize_event(event)` che, dato un evento di
variazione (nuovo/cambiato) rilevato da uno scraper, restituisce sempre un dict
con campi fissi:

    {
        "summary":    str,   # max 400 caratteri, tono tecnico-giuridico
        "category":   str,   # una tra le categorie ammesse
        "relevance":  str,   # "low" | "medium" | "high"
        "key_points": [str], # punti chiave (0-5 stringhe brevi)
    }

Se la chiamata a Gemini fallisce (rete, quota, parsing), la funzione NON solleva
eccezioni: restituisce un fallback prudente così che la notifica Slack parta
comunque, segnalando che il riassunto AI non è disponibile.
"""

import json
import os
import time

import google.generativeai as genai

# Modello di default Complaion. Cambiare solo se giustificato.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# Categorie ammesse: il prompt chiede a Gemini di sceglierne ESATTAMENTE una.
CATEGORIES = [
    "Provvedimento sanzionatorio",
    "Provvedimento / Decisione",
    "Linee guida",
    "Consultazione pubblica",
    "Comunicazione / News",
    "Newsletter",
    "Modifica normativa",
    "Altro",
]

MAX_SUMMARY_CHARS = 400

_configured = False


def _ensure_configured():
    """Configura l'SDK Gemini una sola volta usando la key da ambiente."""
    global _configured
    if _configured:
        return
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY non impostata. Configurarla come GitHub Secret."
        )
    genai.configure(api_key=api_key)
    _configured = True


def _build_prompt(event):
    """Costruisce il prompt strutturato calibrato sul contesto normativo."""
    authority = event.get("source_label", "Autorità non specificata")
    title = event.get("title", "")
    date = event.get("date", "")
    url = event.get("url", "")
    # Testo grezzo eventualmente disponibile (descrizione RSS, badge EDPB, ecc.)
    raw = event.get("raw_text", "") or ""
    extra = event.get("extra", "") or ""
    change_type = event.get("change_type", "new")

    categorie = "\n".join(f"- {c}" for c in CATEGORIES)

    return f"""Sei un assistente di compliance privacy di Complaion che assiste
auditor e consulenti. Devi riassumere una variazione rilevata su una fonte
normativa in materia di protezione dei dati personali (GDPR/RGPD).

TONO RICHIESTO: tecnico-giuridico, orientato all'impatto di compliance.
Sii preciso, neutro e sobrio. NON inventare informazioni non presenti nel
contenuto fornito: se un dato non c'è, non dedurlo. Scrivi in ITALIANO anche
se la fonte è in spagnolo o inglese.

FONTE (autorità): {authority}
TIPO DI VARIAZIONE: {change_type}
TITOLO: {title}
DATA: {date}
URL: {url}
CONTENUTO/METADATI DISPONIBILI: {raw}
NOTE AGGIUNTIVE: {extra}

Devi restituire ESCLUSIVAMENTE un oggetto JSON valido (nessun testo fuori dal
JSON) con questi campi:

{{
  "summary": "riassunto in italiano, MASSIMO {MAX_SUMMARY_CHARS} caratteri, che dica cosa è successo e perché rileva per la compliance",
  "category": "una ed una sola tra le seguenti categorie: {', '.join(CATEGORIES)}",
  "relevance": "low | medium | high — valuta la rilevanza per un auditor GDPR (high = impatto operativo/normativo diretto; medium = utile da conoscere; low = informativo/istituzionale)",
  "key_points": ["fino a 5 punti chiave molto brevi; lista vuota se non ce ne sono di significativi"]
}}

Categorie ammesse:
{categorie}
"""


def _fallback(event, reason):
    """Risultato prudente quando Gemini non è disponibile."""
    title = event.get("title", "")
    return {
        "summary": f"[Riassunto AI non disponibile: {reason}] {title}"[:MAX_SUMMARY_CHARS],
        "category": "Altro",
        "relevance": "medium",
        "key_points": [],
        "ai_ok": False,
    }


def _coerce(result, event):
    """Valida e normalizza l'output del modello."""
    summary = str(result.get("summary", "")).strip()
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[: MAX_SUMMARY_CHARS - 1].rstrip() + "…"

    category = str(result.get("category", "Altro")).strip()
    if category not in CATEGORIES:
        category = "Altro"

    relevance = str(result.get("relevance", "medium")).strip().lower()
    if relevance not in ("low", "medium", "high"):
        relevance = "medium"

    key_points = result.get("key_points", [])
    if not isinstance(key_points, list):
        key_points = []
    key_points = [str(k).strip() for k in key_points if str(k).strip()][:5]

    if not summary:
        summary = event.get("title", "")[:MAX_SUMMARY_CHARS]

    return {
        "summary": summary,
        "category": category,
        "relevance": relevance,
        "key_points": key_points,
        "ai_ok": True,
    }


def summarize_event(event, max_retries=2, retry_sleep=4):
    """Riassume e classifica un singolo evento. Non solleva mai eccezioni."""
    try:
        _ensure_configured()
    except Exception as exc:  # key mancante: fallback esplicito
        return _fallback(event, str(exc))

    prompt = _build_prompt(event)
    model = genai.GenerativeModel(GEMINI_MODEL)
    generation_config = {
        "temperature": 0.2,
        "response_mime_type": "application/json",
    }

    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = model.generate_content(
                prompt, generation_config=generation_config
            )
            text = (resp.text or "").strip()
            result = json.loads(text)
            return _coerce(result, event)
        except Exception as exc:  # rete, quota, parsing JSON
            last_err = exc
            if attempt < max_retries:
                time.sleep(retry_sleep * (attempt + 1))  # backoff lineare
            continue

    return _fallback(event, f"errore Gemini: {last_err}")
