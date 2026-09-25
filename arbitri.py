import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

ROME = ZoneInfo("Europe/Rome")
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "designazioni.json"

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

TIMEOUT = 30
RETRIES = 4

UEFA = {
    "Champions League": "https://it.uefa.com/uefachampionsleague/clubs/50139/matches/",
    "Europa League": "https://it.uefa.com/uefaeuropaleague/clubs/50139--juventus/matches/",
    "Conference League": "https://it.uefa.com/uefaeuropaconferenceleague/clubs/50139--juventus/matches/",
}

AIA = "https://www.aia-figc.it/news/?c=9"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
})

# Le pagine it.uefa.com sono dietro un anti-bot (Akamai) che fa scadere in
# timeout le richieste "requests"-style. Per queste usiamo un browser reale
# via Playwright; per AIA e Telegram restiamo su requests (nessun problema
# riscontrato lì).
PLAYWRIGHT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Etichette allineate a quelle realmente usate da it.uefa.com
# (Quarto uomo, non "quarto ufficiale"; VAR/AVAR con dicitura completa;
# AVAR PRIMA di VAR perché "Video Assistant Referee" è una sottostringa
# di "Assistente Video Assistant Referee")
ROLE_PATTERNS = [
    ("ARBITRO", "arbitro"),
    ("ASSISTENTI", "assistenti arbitrali"),
    ("AVAR", "assistente video assistant referee"),
    ("VAR", "video assistant referee"),
    ("IV", "quarto uomo"),
]


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def request(url):
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            print(f"[HTTP] {attempt}/{RETRIES} {url}")
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last = exc
            print(f"[HTTP] errore: {exc}")
            if attempt < RETRIES:
                time.sleep(attempt * 4)
    raise RuntimeError(f"richiesta fallita: {url} -> {last}")


def render_url(page, url):
    """Carica url con un browser reale (Playwright) e ritorna l'HTML renderizzato.
    Serve per it.uefa.com, che con 'requests' va spesso in timeout (anti-bot)."""
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            print(f"[PLAYWRIGHT] {attempt}/{RETRIES} {url}")
            page.goto(url, wait_until="networkidle", timeout=TIMEOUT * 1000)
            return page.content()
        except Exception as exc:
            last = exc
            print(f"[PLAYWRIGHT] errore: {exc}")
            if attempt < RETRIES:
                time.sleep(attempt * 4)
    raise RuntimeError(f"richiesta fallita (playwright): {url} -> {last}")


def load_state():
    if not STATE_FILE.exists():
        return {"uefa": {}, "italia": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        data.setdefault("uefa", {})
        data.setdefault("italia", {})
        return data
    except Exception:
        return {"uefa": {}, "italia": {}}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def send_telegram(text):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Secret mancanti: configura TELEGRAM_TOKEN e CHAT_ID.")
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    if not r.json().get("ok"):
        raise RuntimeError(str(r.json()))


def match_id(url):
    m = re.search(r"/match/(\d+)", url)
    return m.group(1) if m else None


def find_match_links(soup):
    found = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/match/" not in href:
            continue
        mid = match_id(href)
        if not mid:
            continue
        href = href.split("?", 1)[0].split("#", 1)[0]
        if "/matchinfo" not in href:
            href = href.rstrip("/") + "/matchinfo/"
        found.add(urljoin("https://it.uefa.com", href))
    return sorted(found)


def extract_roles(soup):
    # IMPORTANTE: rimuovere script/style prima di estrarre il testo.
    # Senza questo, il testo di eventuali blob JSON incorporati nella
    # pagina (Next.js/__NEXT_DATA__ ecc.) finisce nel testo estratto e
    # può generare match spuri (es. "ARBITRO: Referee" invece del nome).
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = clean(soup.get_text(" ", strip=True))

    # Restringi la ricerca alla sola sezione "Arbitri", tra il titolo
    # della sezione e quello successivo ("Cartelle stampa partita").
    # Così eventuale rumore altrove nella pagina (nav, footer, meta)
    # non può interferire.
    m = re.search(r"\bArbitri\b(.*?)(?:\bCartelle stampa partita\b|$)", text, re.S)
    section = clean(m.group(1)) if m else text

    boundary = "|".join(re.escape(alias) for _, alias in ROLE_PATTERNS)
    result = {}
    remaining = section
    for role, alias in ROLE_PATTERNS:
        pattern = rf"\b{re.escape(alias)}\b\s*[:\-]?\s*(.+?)(?=\s+(?:{boundary})\b|$)"
        match = re.search(pattern, remaining, re.I)
        if match:
            value = clean(match.group(1)).strip(":- ")
            if value:
                result[role] = value
                # Rimuovi lo span trovato dal testo residuo: evita che,
                # ad es., il match di VAR "mangi" pezzi già assegnati ad AVAR
                # (dato che una frase è sottostringa dell'altra).
                remaining = remaining[: match.start()] + remaining[match.end():]

    return result


def title_from_soup(soup):
    # Priorità a h1/h2 (es. "Juventus vs N.E.C."), che sono più puliti
    # del tag <title> della pagina (che include "| Info partita | UEFA
    # Europa League 2026/27 | UEFA.com" e rovinava l'hashtag).
    for tag in soup.find_all(["h1", "h2"]):
        value = clean(tag.get_text(" ", strip=True))
        if "juventus" in value.lower() and len(value) < 180:
            return value

    title_tag = soup.find("title")
    if title_tag:
        value = clean(title_tag.get_text(" ", strip=True)).split("|")[0].strip()
        if "juventus" in value.lower() and len(value) < 180:
            return value

    return "Juventus"


COUNTRY_FLAGS = {
    "ALB": "🇦🇱", "AND": "🇦🇩", "ARM": "🇦🇲", "AUT": "🇦🇹", "AZE": "🇦🇿",
    "BEL": "🇧🇪", "BIH": "🇧🇦", "BLR": "🇧🇾", "BUL": "🇧🇬", "CRO": "🇭🇷",
    "CYP": "🇨🇾", "CZE": "🇨🇿", "DEN": "🇩🇰", "ESP": "🇪🇸", "EST": "🇪🇪",
    "FIN": "🇫🇮", "FRA": "🇫🇷", "GEO": "🇬🇪", "GER": "🇩🇪", "GIB": "🇬🇮",
    "GRE": "🇬🇷", "HUN": "🇭🇺", "IRL": "🇮🇪", "ISL": "🇮🇸", "ISR": "🇮🇱",
    "ITA": "🇮🇹", "KAZ": "🇰🇿", "KOS": "🇽🇰", "LAT": "🇱🇻", "LIE": "🇱🇮",
    "LTU": "🇱🇹", "LUX": "🇱🇺", "MDA": "🇲🇩", "MKD": "🇲🇰", "MLT": "🇲🇹",
    "MNE": "🇲🇪", "NED": "🇳🇱", "NOR": "🇳🇴", "POL": "🇵🇱", "POR": "🇵🇹",
    "ROU": "🇷🇴", "RUS": "🇷🇺", "SMR": "🇸🇲", "SRB": "🇷🇸", "SUI": "🇨🇭",
    "SVK": "🇸🇰", "SVN": "🇸🇮", "SWE": "🇸🇪", "TUR": "🇹🇷", "UKR": "🇺🇦",
    # Nazionali del Regno Unito, che nel calcio hanno codici propri
    # (non ISO) e bandiere Unicode "tag sequence" dedicate:
    "ENG": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "SCO": "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "WAL": "🏴󠁧󠁢󠁷󠁬󠁳󠁿", "NIR": "🇬🇧",
}


def split_officials(text):
    # Nei ruoli con più persone (es. ASSISTENTI) il testo estratto è
    # "Nome Cognome COD Nome Cognome COD" senza separatore: inseriamo un
    # trattino tra un codice nazione e il nome successivo.
    return re.sub(r"([A-Z]{3})\s+(?=[A-Z][a-z])", r"\1 - ", text or "")


def add_flags(text):
    def repl(m):
        code = m.group(0)
        flag = COUNTRY_FLAGS.get(code)
        # Se conosciamo la bandiera, sostituiamo il codice (es. "UKR")
        # con la sola emoji; se non la conosciamo, lasciamo il codice
        # testuale così l'informazione non si perde.
        return flag if flag else code
    return re.sub(r"\b[A-Z]{3}\b", repl, text or "")


def hashtag(title):
    title = re.sub(r"\b(?:vs|v)\b", "", title, flags=re.I)
    title = re.sub(r"[^A-Za-z0-9À-ÖØ-öø-ÿ]", "", title)
    return title or "Juve"


def format_message(prefix, title, roles):
    lines = [f"{prefix} Designazione arbitrale di #{hashtag(title)}:", ""]
    for role in ("ARBITRO", "ASSISTENTI", "IV", "VAR", "AVAR"):
        if roles.get(role):
            lines.append(f"{role}: {add_flags(split_officials(roles[role]))}")
    return "\n".join(lines)


def check_uefa(state):
    changed = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=PLAYWRIGHT_UA,
            locale="it-IT",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        try:
            for competition, calendar in UEFA.items():
                try:
                    html = render_url(page, calendar)
                    soup = BeautifulSoup(html, "html.parser")
                except Exception as exc:
                    print(f"[UEFA] {competition}: {exc}")
                    continue

                links = find_match_links(soup)
                print(f"[UEFA] {competition}: {len(links)} partite trovate")

                for url in links:
                    mid = match_id(url)
                    if not mid:
                        continue
                    key = f"{competition}:{mid}"
                    if key in state["uefa"]:
                        print(f"[SKIP] {key}")
                        continue

                    try:
                        match_html = render_url(page, url)
                        match_page = BeautifulSoup(match_html, "html.parser")
                    except Exception as exc:
                        print(f"[UEFA] match {mid}: {exc}")
                        continue

                    text = clean(match_page.get_text(" ", strip=True))
                    if "juventus" not in text.lower():
                        continue

                    roles = extract_roles(match_page)
                    if not roles.get("ARBITRO"):
                        continue

                    mancanti = [r for r in ("ASSISTENTI", "IV", "VAR", "AVAR") if not roles.get(r)]
                    if mancanti:
                        print(f"[UEFA] Designazione incompleta, mancanti: {', '.join(mancanti)}")

                    title = title_from_soup(match_page)
                    message = format_message("🇪🇺ℹ️", title, roles)
                    try:
                        send_telegram(message)
                    except Exception as exc:
                        print(f"[UEFA] Telegram: {exc}")
                        continue

                    state["uefa"][key] = {
                        "competition": competition,
                        "match_id": mid,
                        "match": title,
                        "url": url,
                        "sent_at": datetime.now(ROME).isoformat(),
                    }
                    save_state(state)
                    changed = True
                    print(f"[SENT] {key}")
        finally:
            browser.close()

    return changed


def parse_date(text):
    months = {
        "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
        "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
        "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
    }
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", text)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.search(r"\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})\b", text, re.I)
    if m and m.group(2).lower() in months:
        return f"{m.group(3)}-{months[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    return None


def article_links(soup):
    result = []
    for a in soup.find_all("a", href=True):
        if clean(a.get_text(" ", strip=True)):
            result.append(urljoin(AIA, a["href"]))
    return list(dict.fromkeys(result))


def check_italia(state):
    today = datetime.now(ROME).date().isoformat()
    try:
        index = BeautifulSoup(request(AIA).text, "html.parser")
    except Exception as exc:
        print(f"[ITALIA] AIA: {exc}")
        return False

    changed = False
    for url in article_links(index):
        try:
            soup = BeautifulSoup(request(url).text, "html.parser")
        except Exception:
            continue

        text = clean(soup.get_text(" ", strip=True))
        if parse_date(text) != today or "juventus" not in text.lower():
            continue

        roles = extract_roles(soup)
        if not roles.get("ARBITRO"):
            continue

        key = f"{today}:{url}"
        if key in state["italia"]:
            print(f"[SKIP] {key}")
            continue

        title = title_from_soup(soup)
        message = format_message("🇮🇹ℹ️", title, roles)
        try:
            send_telegram(message)
        except Exception as exc:
            print(f"[ITALIA] Telegram: {exc}")
            continue

        state["italia"][key] = {
            "date": today,
            "match": title,
            "url": url,
            "sent_at": datetime.now(ROME).isoformat(),
        }
        save_state(state)
        changed = True
        print(f"[SENT] {key}")
    return changed


def main():
    state = load_state()
    print(f"[START] {datetime.now(ROME).isoformat()}")
    check_uefa(state)
    check_italia(state)
    print("[DONE] Controllo completato.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERRORE: {exc}")
        sys.exit(1)
