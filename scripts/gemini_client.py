# -*- coding: utf-8 -*-
"""
gemini_client.py — Riassunto e classificazione AI dei contenuti normativi (Gemini).

Usa l'SDK Google moderno `google-genai` (compatibile col nuovo formato di API
key, che inizia con "AQ."). Espone `summarize_event(event)` che restituisce
sempre un dict con campi fissi:

    {"summary": str(<=400), "category": str, "relevance": "low|medium|high",
     "key_points": [str], "ai_ok": bool}

Se la chiamata a Gemini fallisce (rete, quota, key mancante/invalida, parsing),
la funzione NON solleva eccezioni: restituisce un fallback prudente così che la
notifica Slack e la dashboard funzionino comunque.
"""

import json
import os
import time

from google import genai

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

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

_client = None


def _get_client():
    """Crea (una volta) il client Gemini con la API key da ambiente."""
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY non impostata. Configurarla come GitHub Secret.")
        _client = genai.Client(api_key=api_key)
    return _client


def _build_prompt(event):
    authority = event.get("source_label", "Autorità non specificata")
    title = event.get("title", "")
    date = event.get("date", "")
    url = event.get("url", "")
    raw = event.get("raw_text", "") or ""
    extra = event.get("extra", "") or ""
    change_type = event.get("change_type", "new")
    categorie = "\n".join(f"- {c}" for c in CATEGORIES)

    return f"""Sei un assistente di compliance privacy di Complaion che assiste
auditor e consulenti. Devi riassumere una variazione rilevata su una fonte
normativa in materia di protezione dei dati personali (GDPR/RGPD).

TONO RICHIESTO: tecnico-giuridico, orientato all'impatto di compliance.
Sii preciso, neutro e sobrio. NON inventare informazioni non presenti nel
contenuto fornito. Scrivi in ITALIANO anche se la fonte è in spagnolo o inglese.

FONTE (autorità): {authority}
TIPO DI VARIAZIONE: {change_type}
TITOLO: {title}
DATA: {date}
URL: {url}
CONTENUTO/METADATI DISPONIBILI: {raw}
NOTE AGGIUNTIVE: {extra}

Restituisci ESCLUSIVAMENTE un oggetto JSON valido (nessun testo fuori dal JSON):

{{
  "summary": "riassunto in italiano, MAX {MAX_SUMMARY_CHARS} caratteri, cosa è successo e perché rileva per la compliance",
  "category": "una sola tra: {', '.join(CATEGORIES)}",
  "relevance": "low | medium | high (rilevanza per un auditor GDPR)",
  "key_points": ["fino a 5 punti chiave brevi; lista vuota se non significativi"]
}}

Categorie ammesse:
{categorie}
"""


def _fallback(event, reason):
    title = event.get("title", "")
    return {
        "summary": f"[Riassunto AI non disponibile: {reason}] {title}"[:MAX_SUMMARY_CHARS],
        "category": "Altro",
        "relevance": "medium",
        "key_points": [],
        "ai_ok": False,
    }


def _coerce(result, event):
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
        client = _get_client()
    except Exception as exc:
        return _fallback(event, str(exc))

    prompt = _build_prompt(event)
    config = {"temperature": 0.2, "response_mime_type": "application/json"}

    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL, contents=prompt, config=config)
            text = (resp.text or "").strip()
            result = json.loads(text)
            return _coerce(result, event)
        except Exception as exc:
            last_err = exc
            if attempt < max_retries:
                time.sleep(retry_sleep * (attempt + 1))
            continue

    return _fallback(event, f"errore Gemini: {last_err}")
