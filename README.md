# complaion-gdpr-monitor

Monitor automatico delle novità in materia di **GDPR / RGPD**. Ogni giorno
scarica i contenuti da quattro fonti normative, rileva le variazioni rispetto
all'ultimo scan, ne genera un riassunto AI con classificazione e le notifica su
Slack. Segue il pattern tecnico Complaion dei monitor `complaion-acn-monitor` e
`complaion-dora-monitor`.

## Fonti monitorate

| ID sorgente          | Autorità / fonte                          | Metodo            |
|----------------------|-------------------------------------------|-------------------|
| `aepd`               | AEPD — Agencia Española de Protección de Datos | Feed RSS      |
| `garante_comunicati` | Garante Privacy (IT) — comunicati stampa  | Feed RSS          |
| `garante_newsletter` | Garante Privacy (IT) — newsletter         | Feed RSS          |
| `edpb`               | EDPB — European Data Protection Board     | RSS autodiscovery + fallback HTML |
| `eurlex`             | EUR-Lex — Reg. (UE) 2016/679 (GDPR)       | Sentinella versione consolidata |

**Nota su EUR-Lex:** non è una fonte di "news" ma il testo normativo. Il monitor
controlla la **versione consolidata** del GDPR e segnala una variazione solo
quando il testo viene effettivamente emendato (nuova data di consolidazione o
nuovi atti modificativi). È normale che questa fonte riporti "0 variazioni" per
lunghi periodi: è il comportamento atteso.

## Come funziona

1. **Scarica e parsifica** ogni fonte (`scripts/scrape_gdpr.py`).
2. **Confronta** con `docs/data/last_scan.json` per rilevare item *nuovi* e
   *aggiornati*. Gli item che escono dai feed news **non** vengono segnalati
   come "rimossi" (uscirebbero naturalmente dal feed → sarebbe solo rumore).
3. Per ogni variazione chiama **Gemini** (`scripts/gemini_client.py`) che
   restituisce un JSON con `summary` (max 400 caratteri), `category`,
   `relevance` (low/medium/high) e `key_points`. Tono **tecnico-giuridico**.
4. **Notifica su Slack** (`scripts/notify_slack.py`) con payload Block Kit:
   header + riepilogo + una sezione per evento (max 20 inline, poi
   "+ altre N variazioni"). Eventuali errori di scraping compaiono in un blocco
   di **attenzione** ben visibile.
5. **Committa** il nuovo `last_scan.json` su `docs/data/`. Lo storico dei commit
   (autore `github-actions[bot]`) è la memoria del monitor.

### Primo scan (baseline)

Al primissimo run non esiste uno stato precedente: tutti gli item attuali delle
fonti vengono elencati come *nuovi* per creare la baseline. Per contenere i
costi, al primo run i riassunti AI sono limitati ai 5 item più recenti per
fonte; gli altri sono elencati con solo il titolo. Dal secondo run in poi
vengono segnalate solo le variazioni reali (di norma poche) e tutte riassunte.

## Dashboard (GitHub Pages)

Il repo include una dashboard web (`docs/index.html`) che si aggiorna da sola a
ogni scan. Mostra le metriche (item tracciati, variazioni 7/30 gg, fonti), un
grafico dell'andamento a 30 giorni, e una tabella filtrabile e ordinabile per
fonte, categoria e rilevanza, con dark mode. È statica e legge lato client i
dati in `docs/data/dashboard.json` prodotti dallo scraper — nessun server.

Per pubblicarla: **Settings → Pages → Build and deployment → Source: Deploy
from a branch → Branch: `main` / cartella `/docs`**. L'URL sarà del tipo
`https://<utente>.github.io/<repo>/`.

## Struttura del repository

```
complaion-gdpr-monitor/
├── .github/workflows/monitor.yml   # GitHub Action (cron + esecuzione manuale)
├── scripts/
│   ├── requirements.txt            # dipendenze Python (versioni pinnate)
│   ├── scrape_gdpr.py              # scraper multi-fonte + diff + orchestrazione
│   ├── gemini_client.py            # riassunto/classificazione AI (Gemini)
│   └── notify_slack.py             # notificatore Slack (Block Kit)
├── docs/
│   ├── index.html                  # dashboard (GitHub Pages)
│   ├── style.css                   # stile dashboard (light/dark)
│   ├── app.js                      # logica dashboard (Chart.js, filtri, tabella)
│   └── data/
│       ├── last_scan.json          # stato/diff arricchito (creato al primo run)
│       ├── dashboard.json          # dati appiattiti per la dashboard
│       └── history.json            # serie storica per il grafico trend
├── README.md
└── .gitignore
```

## Segreti richiesti (GitHub Secrets)

Da impostare in **Settings → Secrets and variables → Actions**:

| Secret               | Descrizione                                  |
|----------------------|----------------------------------------------|
| `GEMINI_API_KEY`     | API key di Google AI Studio (Gemini)         |
| `SLACK_WEBHOOK_URL`  | URL dell'Incoming Webhook Slack del canale   |

I segreti **non** vanno mai scritti nel codice.

## Attivare / disattivare il monitor

- **Attivare:** Actions → workflow "Monitor GDPR" → *Enable workflow*. Un
  workflow appena aggiunto è disabilitato finché non lo si abilita.
- **Esecuzione manuale (test):** Actions → "Monitor GDPR" → *Run workflow*
  (`workflow_dispatch`).
- **Disattivare temporaneamente:** Actions → "Monitor GDPR" → *Disable workflow*.
- **Cambiare la frequenza:** modificare `on.schedule.cron` in
  `.github/workflows/monitor.yml`. Il valore è in **UTC**
  (default `0 6 * * *` = 06:00 UTC = 08:00 ora legale estiva IT).

## Leggere i log

Actions → run del workflow → job **scan**. Lo scraper stampa una riga per fonte
(`[OK]` / `[ERRORE]`) e un riepilogo finale con conteggi e numero di chiamate
Gemini. Se una fonte è in errore, l'Action viene marcata come **fallita**
(rossa) di proposito, e la stessa segnalazione arriva anche su Slack.

## Aggiungere o cambiare una fonte

Le fonti sono definite nella lista `SOURCES` in `scripts/scrape_gdpr.py`. Ogni
voce ha `id`, `label`, `kind` (`rss` | `edpb_html` | `eurlex`) e `url`. Per una
nuova fonte RSS basta aggiungere una voce con `kind: "rss"`. Per una fonte HTML
serve un parser dedicato (vedi `parse_edpb` come modello). Dopo la modifica,
lanciare un `workflow_dispatch` per verificare.

## Manutenzione e troubleshooting

- **Una fonte va in errore all'improvviso:** i siti `europa.eu` (EDPB, EUR-Lex)
  hanno protezione anti-bot; un blocco temporaneo si risolve al run successivo.
  Se l'errore persiste, la struttura della pagina potrebbe essere cambiata e il
  parser va aggiornato — segnalarlo al team (vedi sotto).
- **Nessuna notifica Slack ma l'Action è verde:** normale se non ci sono
  variazioni (per non generare rumore, con 0 variazioni non si invia nulla).
  Per ricevere comunque un messaggio giornaliero, impostare la variabile
  d'ambiente `SLACK_NOTIFY_ON_NO_CHANGES=true`.
- **Costi/limiti Gemini:** i riassunti sono limitati (max 40 chiamate per run,
  con pausa tra le chiamate). In caso di errore Gemini la notifica parte
  comunque, con l'indicazione "[Riassunto AI non disponibile]".

## Contatti di supporto interni

> ⚠️ **Da completare a cura del team Complaion prima della messa in produzione:**
> - Chi contattare se lo scraper si rompe (cambio struttura sito): _________
> - Chi gestisce le Gemini API key / progetto Google Cloud: _________
> - Chi ha permessi admin sul workspace Slack Complaion: _________

## Riferimenti

Monitor Complaion esistenti come riferimento in caso di dubbi:
`complaion-acn-monitor`, `complaion-dora-monitor`.
