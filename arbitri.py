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
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last = exc
            print(f"[HTTP] tentativo {attempt}/{RETRIES} fallito ({url}): {exc}")
            if attempt < RETRIES:
                time.sleep(attempt * 4)
    raise RuntimeError(f"richiesta fallita: {url} -> {last}")


def render_url(page, url):
    """Carica url con un browser reale (Playwright) e ritorna l'HTML renderizzato.
    Serve per it.uefa.com, che con 'requests' va spesso in timeout (anti-bot)."""
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            page.goto(url, wait_until="networkidle", timeout=TIMEOUT * 1000)
            return page.content()
        except Exception as exc:
            last = exc
            print(f"[PLAYWRIGHT] tentativo {attempt}/{RETRIES} fallito ({url}): {exc}")
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


def send_telegram(text, entities=None):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Secret mancanti: configura TELEGRAM_TOKEN e CHAT_ID.")
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    if entities:
        payload["entities"] = entities
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json=payload,
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


# Emoji Unicode (fallback per bandiere senza custom_emoji_id: ENG/SCO/WAL/NIR)
COUNTRY_FLAGS = {
    "ROU": "🇷🇴", "RUS": "🇷🇺", "SMR": "🇸🇲", "SRB": "🇷🇸", "SUI": "🇨🇭",
    "SVK": "🇸🇰", "SVN": "🇸🇮", "SWE": "🇸🇪", "TUR": "🇹🇷", "UKR": "🇺🇦",
    "ENG": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "SCO": "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "WAL": "🏴󠁧󠁢󠁷󠁬󠁳󠁿", "NIR": "🇬🇧",
}

# custom_emoji_id per le bandiere UEFA (premium Telegram)
# Ogni entry: codice 3 lettere → (emoji_unicode_di_base, custom_emoji_id)
COUNTRY_CUSTOM_EMOJI: dict[str, tuple[str, str]] = {
    "ALB": ("🇦🇱", "5442808872202942144"),
    "AND": ("🇦🇩", "5229127072336589200"),
    "ARM": ("🇦🇲", "5411455658186778270"),
    "AUT": ("🇦🇹", "5409096965227031137"),
    "AZE": ("🇦🇿", "5224254431939275524"),
    "BEL": ("🇧🇪", "5411564862025244994"),
    "BIH": ("🇧🇦", "5382033281078275575"),
    "BLR": ("🇧🇾", "5382219601054544127"),
    "BUL": ("🇧🇬", "5408875181705799521"),
    "CRO": ("🇭🇷", "5262677003210860950"),
    "CYP": ("🇨🇾", "5228997115216149309"),
    "CZE": ("🇨🇿", "5429496861587156146"),
    "DEN": ("🇩🇰", "5381854399985366436"),
    "ESP": ("🇪🇸", "5201957744877248121"),
    "EST": ("🇪🇪", "5411174505332615466"),
    "FIN": ("🇫🇮", "5382151560182642075"),
    "FRA": ("🇫🇷", "5202132623060640759"),
    "GEO": ("🇬🇪", "5440371950708864925"),
    "GER": ("🇩🇪", "5409360418520967565"),
    "GIB": ("🇬🇮", "5226496954623603888"),
    "GRE": ("🇬🇷", "5381889099026149370"),
    "HUN": ("🇭🇺", "5409065547541260699"),
    "IRL": ("🇮🇪", "5411194670204069594"),
    "ISL": ("🇮🇸", "5226903563472483349"),
    "ISR": ("🇮🇱", "5332299462461107995"),
    "ITA": ("🇮🇹", "5449723275628259037"),
    "KAZ": ("🇰🇿", "5228718354658769982"),
    "KOS": ("🇽🇰", "5442767700646442025"),
    "LAT": ("🇱🇻", "5269650286342846979"),
    "LIE": ("🇱🇮", "5226703795953612903"),
    "LTU": ("🇱🇹", "5411197345968695511"),
    "LUX": ("🇱🇺", "5411158944666101440"),
    "MDA": ("🇲🇩", "5442607966517736672"),
    "MKD": ("🇲🇰", "5442634591020003500"),
    "MLT": ("🇲🇹", "5226954282741283529"),
    "MNE": ("🇲🇪", "5440827745523216914"),
    "NED": ("🇳🇱", "5411124743841524806"),
    "NOR": ("🇳🇴", "5382300771641470186"),
    "POL": ("🇵🇱", "5291847690940852675"),
    "POR": ("🇵🇹", "5382075788369605892"),
}

# Prefissi con custom emoji (testo base + custom_emoji_id)
# Il testo deve contenere esattamente i caratteri Unicode corrispondenti,
# poi le entità indicano l'offset/length per sovrapporre la custom emoji.
PREFIX_ITALIA = ("🇮🇹ℹ️", [
    {"offset": 0, "length": 4, "type": "custom_emoji", "custom_emoji_id": "6048880421830136769"},
    {"offset": 4, "length": 2, "type": "custom_emoji", "custom_emoji_id": "5334544901428229844"},
])
PREFIX_UEFA = ("🇪🇺ℹ️", [
    {"offset": 0, "length": 4, "type": "custom_emoji", "custom_emoji_id": "6048508615101256052"},
    {"offset": 4, "length": 2, "type": "custom_emoji", "custom_emoji_id": "5334544901428229844"},
])


def split_officials(text):
    # Nei ruoli con più persone (es. ASSISTENTI) il testo estratto è
    # "Nome Cognome COD Nome Cognome COD" senza separatore: inseriamo un
    # trattino tra un codice nazione e il nome successivo.
    return re.sub(r"([A-Z]{3})\s+(?=[A-Z][a-z])", r"\1 - ", text or "")


def _utf16_len(s: str) -> int:
    """Lunghezza in unità UTF-16 (come conta Telegram gli offset)."""
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def add_flags_with_entities(text: str) -> tuple[str, list[dict]]:
    """Sostituisce i codici nazione a 3 lettere con le emoji bandiera e
    restituisce (testo_risultante, lista_entità_custom_emoji).

    Le entità hanno offset in unità UTF-16, come richiesto da Telegram.
    Per le nazioni con custom_emoji_id usiamo quella; per le altre (ENG,
    SCO, WAL, NIR, ROU…) usiamo l'emoji Unicode standard senza entità.
    """
    result_chars: list[str] = []
    entities: list[dict] = []
    offset_utf16 = 0  # cursore in unità UTF-16 sul testo risultante

    # Spezziamo il testo nei token: sequenze [A-Z]{3} vs tutto il resto
    parts = re.split(r"(\b[A-Z]{3}\b)", text or "")
    for part in parts:
        if re.fullmatch(r"[A-Z]{3}", part):
            if part in COUNTRY_CUSTOM_EMOJI:
                emoji_char, eid = COUNTRY_CUSTOM_EMOJI[part]
                length = _utf16_len(emoji_char)
                entities.append({
                    "offset": offset_utf16,
                    "length": length,
                    "type": "custom_emoji",
                    "custom_emoji_id": eid,
                })
                result_chars.append(emoji_char)
                offset_utf16 += length
            elif part in COUNTRY_FLAGS:
                emoji_char = COUNTRY_FLAGS[part]
                result_chars.append(emoji_char)
                offset_utf16 += _utf16_len(emoji_char)
            else:
                # Codice sconosciuto: lasciamo il testo così com'è
                result_chars.append(part)
                offset_utf16 += _utf16_len(part)
        else:
            result_chars.append(part)
            offset_utf16 += _utf16_len(part)

    return "".join(result_chars), entities


def hashtag(title):
    # Vogliamo sempre "Juve" + nome dell'avversario, es. "JuveNec",
    # "JuveAtalanta" — non l'intero titolo della pagina.
    parts = [clean(p) for p in re.split(r"\b(?:vs|v|-)\b", title, flags=re.I)]
    parts = [p for p in parts if p]
    opponent = next((p for p in parts if "juventus" not in p.lower()), None)
    if opponent is None:
        opponent = re.sub(r"juventus", "", title, flags=re.I)

    opponent = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ\s]", "", opponent)
    words = [w for w in opponent.split() if w]
    opponent_camel = "".join(w[:1].upper() + w[1:].lower() for w in words)
    return f"Juve{opponent_camel}" if opponent_camel else "Juve"


def format_message(prefix_data: tuple, title: str, roles: dict) -> tuple[str, list[dict]]:
    """Restituisce (testo, entità) pronti per sendMessage con custom emoji.

    prefix_data è una delle costanti PREFIX_ITALIA / PREFIX_UEFA:
        (stringa_prefix, lista_entità_del_prefix)

    Le entità del prefix hanno offset assoluti a partire da 0 (prima riga).
    Le entità delle bandiere vengono aggiunte con offset aggiornati tenendo
    conto del testo che precede la riga di ogni ruolo.
    """
    prefix_text, prefix_entities = prefix_data

    # Prima riga: "<prefix> Designazione arbitrale di #Hashtag:"
    first_line = f"{prefix_text} Designazione arbitrale di #{hashtag(title)}:"
    lines = [first_line, ""]

    # Calcola offset UTF-16 accumulato fino alla fine delle prime due righe
    # (prima riga + "\n" + riga vuota + "\n")
    base_offset = _utf16_len(first_line + "\n" + "\n")

    all_entities: list[dict] = list(prefix_entities)  # copia entità prefix

    for role in ("ARBITRO", "ASSISTENTI", "IV", "VAR", "AVAR"):
        if not roles.get(role):
            continue
        label = f"{role}: "
        flag_text, flag_entities = add_flags_with_entities(split_officials(roles[role]))
        line = label + flag_text
        label_offset = _utf16_len(label)
        # Aggiusta gli offset delle entità bandiera tenendo conto di:
        # base_offset (testo già scritto) + label (es. "ARBITRO: ")
        for ent in flag_entities:
            shifted = dict(ent)
            shifted["offset"] = base_offset + label_offset + ent["offset"]
            all_entities.append(shifted)
        lines.append(line)
        base_offset += _utf16_len(line + "\n")

    return "\n".join(lines), all_entities


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
                    print(f"[UEFA] {competition}: errore nel calendario -> {exc}")
                    continue

                links = find_match_links(soup)
                nuove = 0
                gia_inviate = 0

                for url in links:
                    mid = match_id(url)
                    if not mid:
                        continue
                    key = f"{competition}:{mid}"
                    if key in state["uefa"]:
                        gia_inviate += 1
                        continue

                    try:
                        match_html = render_url(page, url)
                        match_page = BeautifulSoup(match_html, "html.parser")
                    except Exception as exc:
                        print(f"[UEFA] {competition}, match {mid}: errore -> {exc}")
                        continue

                    text = clean(match_page.get_text(" ", strip=True))
                    if "juventus" not in text.lower():
                        continue

                    roles = extract_roles(match_page)
                    if not roles.get("ARBITRO"):
                        continue

                    title = title_from_soup(match_page)
                    mancanti = [r for r in ("ASSISTENTI", "IV", "VAR", "AVAR") if not roles.get(r)]
                    if mancanti:
                        print(f"[UEFA] {title}: designazione incompleta (mancano {', '.join(mancanti)})")

                    message, entities = format_message(PREFIX_UEFA, title, roles)
                    try:
                        send_telegram(message, entities)
                    except Exception as exc:
                        print(f"[UEFA] {title}: invio Telegram fallito -> {exc}")
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
                    nuove += 1
                    print(f"[UEFA] ✅ inviata: {title} ({competition})")

                print(f"[UEFA] {competition}: {len(links)} partite, {gia_inviate} già inviate, {nuove} nuove")
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
        print(f"[ITALIA] errore nell'indice AIA -> {exc}")
        return False

    changed = False
    nuove = 0
    gia_inviate = 0
    articoli = article_links(index)

    for url in articoli:
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
            gia_inviate += 1
            continue

        title = title_from_soup(soup)
        message, entities = format_message(PREFIX_ITALIA, title, roles)
        try:
            send_telegram(message, entities)
        except Exception as exc:
            print(f"[ITALIA] {title}: invio Telegram fallito -> {exc}")
            continue

        state["italia"][key] = {
            "date": today,
            "match": title,
            "url": url,
            "sent_at": datetime.now(ROME).isoformat(),
        }
        save_state(state)
        changed = True
        nuove += 1
        print(f"[ITALIA] ✅ inviata: {title}")

    print(f"[ITALIA] {len(articoli)} articoli controllati, {gia_inviate} già inviate, {nuove} nuove")
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
