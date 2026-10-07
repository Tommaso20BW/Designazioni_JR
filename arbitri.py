import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urljoin
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

# =========================
# UEFA - LOGICA ORIGINALE
# =========================
UEFA = {
    "Champions League": "https://it.uefa.com/uefachampionsleague/clubs/50139/matches/",
    "Europa League": "https://it.uefa.com/uefaeuropaleague/clubs/50139--juventus/matches/",
    "Conference League": "https://it.uefa.com/uefaeuropaconferenceleague/clubs/50139--juventus/matches/",
}

# =========================
# AIA / ITALIA
# =========================
AIA = "https://www.aia-figc.it/news/?c=9"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
})

# Le pagine it.uefa.com sono dietro un anti-bot (Akamai) che fa scadere in
# timeout le richieste "requests"-style. Per queste usiamo un browser reale
# via Playwright; per AIA e Telegram restiamo su requests.
PLAYWRIGHT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Etichette allineate a quelle realmente usate da it.uefa.com.
# QUESTA PARTE RESTA DEDICATA A UEFA.
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


# =========================
# UEFA - FUNZIONI ORIGINALI
# =========================
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
    # IMPORTANTE: questa è la funzione usata dal percorso UEFA.
    # Non modificarne il comportamento.
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = clean(soup.get_text(" ", strip=True))
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
                remaining = remaining[: match.start()] + remaining[match.end():]
    return result


def title_from_soup(soup):
    # Funzione originale UEFA.
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


# =========================
# EMOJI / FORMAT - ORIGINALE UEFA
# =========================
COUNTRY_FLAGS: dict[str, str] = {}
COUNTRY_CUSTOM_EMOJI: dict[str, tuple[str, str]] = {
    "ALB": ("🇦🇱", "5442808872202942144"),
    "AND": ("🇦🇩", "5229127072336589200"),
    "ARM": ("🇦🇲", "5411455658186778270"),
    "AUT": ("🇦🇹", "5409096965227031137"),
    "AZE": ("🇦🇿", "5224254431939275524"),
    "BEL": ("🇧🇪", "5411564862025244994"),
    "BIH": ("🇧🇦", "5382033281078275575"),
    "BLR": ("🇧🇾", "5382219600944895243"),
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
    "NOR": ("🇳🇴", "5382300779083549634"),
    "POL": ("🇵🇱", "5291847690940852675"),
    "POR": ("🇵🇹", "5382075788369605892"),
    "ROU": ("🇷🇴", "5411159898148840778"),
    "RUS": ("🇷🇺", "5449408995691341691"),
    "SMR": ("🇸🇲", "5228954998766843234"),
    "SRB": ("🇷🇸", "5384313376136507326"),
    "SUI": ("🇨🇭", "5442703336266543270"),
    "SVK": ("🇸🇰", "5381967160056755878"),
    "SVN": ("🇸🇮", "5440874620796284751"),
    "SWE": ("🇸🇪", "5384542551296455687"),
    "TUR": ("🇹🇷", "5226948110873278599"),
    "UKR": ("🇺🇦", "5447309366568953338"),
    "ENG": ("🏴󠁧󠁢󠁥󠁮󠁧󠁿", "5229192892710402006"),
    "SCO": ("🏴󠁧󠁢󠁳󠁣󠁴󠁿", "5226852401822057871"),
    "WAL": ("🏴󠁧󠁢󠁷󠁬󠁳󠁿", "5228957348113955582"),
    "NIR": ("🇬🇧", "5202196682497859879"),
}

PREFIX_ITALIA = ("🇮🇹ℹ️", [
    {"offset": 0, "length": 4, "type": "custom_emoji", "custom_emoji_id": "6048880421830136769"},
    {"offset": 4, "length": 2, "type": "custom_emoji", "custom_emoji_id": "5334544901428229844"},
])

PREFIX_UEFA = ("🇪🇺ℹ️", [
    {"offset": 0, "length": 4, "type": "custom_emoji", "custom_emoji_id": "6048508615101256052"},
    {"offset": 4, "length": 2, "type": "custom_emoji", "custom_emoji_id": "5334544901428229844"},
])


def split_officials(text):
    return re.sub(r"([A-Z]{3})\s+(?=[A-Z][a-z])", r"\1 - ", text or "")


def _utf16_len(s: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def add_flags_with_entities(text: str) -> tuple[str, list[dict]]:
    result_chars: list[str] = []
    entities: list[dict] = []
    offset_utf16 = 0
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
                result_chars.append(part)
                offset_utf16 += _utf16_len(part)
        else:
            result_chars.append(part)
            offset_utf16 += _utf16_len(part)

    return "".join(result_chars), entities


def hashtag(title):
    # FUNZIONE ORIGINALE UEFA: invariata.
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
    # FUNZIONE ORIGINALE UEFA: invariata.
    prefix_text, prefix_entities = prefix_data
    first_line = f"{prefix_text} Designazione arbitrale di #{hashtag(title)}:"
    lines = [first_line, ""]
    base_offset = _utf16_len(first_line + "\n" + "\n")
    all_entities: list[dict] = list(prefix_entities)

    for role in ("ARBITRO", "ASSISTENTI", "IV", "VAR", "AVAR"):
        if not roles.get(role):
            continue
        label = f"{role}: "
        flag_text, flag_entities = add_flags_with_entities(split_officials(roles[role]))
        line = label + flag_text
        label_offset = _utf16_len(label)
        for ent in flag_entities:
            shifted = dict(ent)
            shifted["offset"] = base_offset + label_offset + ent["offset"]
            all_entities.append(shifted)
        lines.append(line)
        base_offset += _utf16_len(line + "\n")

    return "\n".join(lines), all_entities


# =========================
# AIA / ITALIA - PARSER ROBUSTO
# =========================
# IMPORTANTE: questo blocco è separato dalla logica UEFA.
#
# La ricerca interna AIA con ?cerca=... restituisce attualmente HTTP 400.
# Per questo NON viene più usata. L'archivio categoria ?c=9 è la sorgente
# principale e viene paginato in modo esplicito.
TEAM_SEPARATOR_RE = re.compile(r"\s*[–—-]\s*")
DATE_SUFFIX_RE = re.compile(
    r"\s+(?:(?:Venerdì|Sabato|Domenica|Lunedì|Martedì|Mercoledì|Giovedì)\b.*|"
    r"h\.?\s*\d{1,2}(?::\d{2})?.*|\d{1,2}/\d{1,2}\b.*)$",
    re.I,
)

# Copre le occasioni in cui il workflow è rimasto fermo per alcuni giorni.
# Non si usa più la condizione "pubblicato oggi", perché una designazione AIA
# può essere pubblicata il giorno precedente e il workflow può eseguirsi dopo.
ITALIA_LOOKBACK_DAYS = 45
AIA_CATEGORY_PAGES = 6


def soup_text_lines(soup):
    """Estrae stringhe visibili e rimuove BOM/footer rumorosi."""
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    lines = []
    for raw in soup.stripped_strings:
        value = clean(raw).replace("\ufeff", "")
        if value:
            lines.append(value)
    return lines


def _looks_like_italia_designation_title(value):
    value = clean(value).replace("\ufeff", "")
    upper = value.upper()
    return (
        "SERIE A ENILIVE" in upper
        and "DESIGNAZIONI" in upper
        and "VARIAZIONE" not in upper
    )


def article_text_lines(soup):
    """Restituisce solo il corpo utile dell'articolo AIA.

    L'header/menu/footer della pagina AIA contiene molto testo non pertinente.
    Partiamo dal titolo della designazione e ci fermiamo prima del footer.
    """
    lines = soup_text_lines(soup)
    start = None

    for i, line in enumerate(lines):
        if _looks_like_italia_designation_title(line):
            start = i
            break

    if start is None:
        return lines

    useful = []
    footer_markers = (
        "(aut. Tribunale di Roma n. 499 del 01/09/1989)",
        "Via Campania 47",
        "Copyrights ©",
        "Copyright ©",
        "Privacy Policy / Cookie Policy",
    )

    for line in lines[start:]:
        if any(marker.lower() in line.lower() for marker in footer_markers):
            break
        useful.append(line)

    return useful


def normalize_official_name(name: str) -> str:
    """AIA: capitalizzazione normale del nome.

    MARCENARO -> Marcenaro
    ROSSI C.   -> Rossi C.
    LO CICERO  -> Lo Cicero
    FERRIERI CAPUTI -> Ferrieri Caputi
    """
    name = clean(name)
    name = re.sub(r"\s*\(foto\)\s*", "", name, flags=re.I)
    name = name.strip(" -–—")

    tokens = []
    for token in name.split():
        if not token:
            continue
        pieces = re.split(r"([\-'’])", token)
        normalized = []
        for piece in pieces:
            if piece in {"-", "'", "’"}:
                normalized.append(piece)
            elif piece:
                normalized.append(piece[:1].upper() + piece[1:].lower())
        tokens.append("".join(normalized))

    return " ".join(tokens)


def _strip_match_date(line: str) -> str:
    line = clean(line).replace("\ufeff", "")
    return DATE_SUFFIX_RE.sub("", line).strip()


def _next_nonempty(lines, start, limit=20):
    """Ritorna (indice, valore) della prima stringa utile dopo start."""
    end = min(len(lines), start + limit)
    for i in range(start, end):
        value = clean(lines[i])
        if value:
            return i, value
    return None, ""


def _read_aia_role(lines, start, role):
    """Legge un ruolo AIA sia in forma 'IV: DOVERI' sia su due nodi 'IV:'/'DOVERI'."""
    pattern = re.compile(rf"^{re.escape(role)}\s*:\s*(.*)$", re.I)
    label_only = re.compile(rf"^{re.escape(role)}\s*:\s*$", re.I)

    for i in range(start, min(len(lines), start + 12)):
        value = clean(lines[i])
        if not value:
            continue

        m = pattern.match(value)
        if m:
            inline = clean(m.group(1))
            if inline:
                return i + 1, inline
            j, next_value = _next_nonempty(lines, i + 1, limit=4)
            if j is not None:
                return j + 1, next_value

        if label_only.match(value):
            j, next_value = _next_nonempty(lines, i + 1, limit=4)
            if j is not None:
                return j + 1, next_value

    return start, ""


def extract_italia_assignment(soup):
    """Estrae il solo blocco della gara della Juventus dalla pagina AIA."""
    lines = article_text_lines(soup)

    for idx, raw_line in enumerate(lines):
        line = clean(raw_line).replace("—", "–")

        if not re.search(r"\bJUVENTUS\b", line, re.I):
            continue
        if not TEAM_SEPARATOR_RE.search(line):
            continue
        if len(line) > 180:
            continue

        teams_part = _strip_match_date(line)
        pieces = TEAM_SEPARATOR_RE.split(teams_part, maxsplit=1)
        if len(pieces) != 2:
            continue

        home, away = (clean(p) for p in pieces)
        if home.lower() == away.lower():
            continue
        if "juventus" not in {home.lower(), away.lower()}:
            continue

        # La struttura AIA è posizionale:
        # gara / arbitro / assistenti / IV / VAR / AVAR.
        referee_idx, referee = _next_nonempty(lines, idx + 1, limit=6)
        if referee_idx is None or not referee:
            continue

        # '(foto)' può comparire attaccato o come nodo separato.
        if referee.lower() == "(foto)":
            referee_idx, referee = _next_nonempty(lines, referee_idx + 1, limit=4)
            if referee_idx is None or not referee:
                continue

        assistants_idx, assistants = _next_nonempty(lines, referee_idx + 1, limit=6)
        if assistants_idx is None or not assistants:
            continue
        if re.match(r"^(IV|VAR|AVAR)\s*:", assistants, re.I):
            continue

        role_pos = assistants_idx + 1
        role_pos, iv = _read_aia_role(lines, role_pos, "IV")
        role_pos, var = _read_aia_role(lines, role_pos, "VAR")
        role_pos, avar = _read_aia_role(lines, role_pos, "AVAR")

        if not (referee and assistants and iv and var and avar):
            continue

        assistant_names = []
        for part in re.split(r"\s*[–—-]\s*", assistants):
            part = clean(part)
            if part:
                assistant_names.append(normalize_official_name(part))

        roles = {
            "ARBITRO": normalize_official_name(referee),
            "ASSISTENTI": " – ".join(assistant_names),
            "IV": normalize_official_name(iv),
            "VAR": normalize_official_name(var),
            "AVAR": normalize_official_name(avar),
        }

        # Team names puliti: niente giorno/data/orario.
        title = f"{normalize_team_name(home)} – {normalize_team_name(away)}"
        return title, roles

    return None, {}


def normalize_team_name(name: str) -> str:
    """Capitalizzazione leggibile dei nomi squadra senza alterare sigle iniziali."""
    name = clean(name)
    words = []
    for word in name.split():
        if len(word) <= 2 and word.upper() == word:
            words.append(word.upper())
        elif "." in word and len(word) <= 4:
            words.append(word.upper())
        else:
            words.append(word[:1].upper() + word[1:].lower())
    return " ".join(words)


def hashtag_italia(title: str) -> str:
    """Italia: avversaria + Juve, es. CagliariJuve."""
    parts = TEAM_SEPARATOR_RE.split(clean(title), maxsplit=1)
    if len(parts) != 2:
        opponent = re.sub(r"\bjuventus\b", "", title, flags=re.I)
    else:
        left, right = (clean(p) for p in parts)
        opponent = right if left.lower() == "juventus" else left

    opponent = re.sub(r"\bjuventus\b", "", opponent, flags=re.I)
    opponent = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ0-9\s]", " ", opponent)
    words = [w for w in opponent.split() if w]
    opponent_camel = "".join(w[:1].upper() + w[1:].lower() for w in words)
    return f"{opponent_camel}Juve" if opponent_camel else "Juve"


def format_message_italia(title: str, roles: dict) -> tuple[str, list[dict]]:
    prefix_text, prefix_entities = PREFIX_ITALIA
    first_line = f"{prefix_text} Designazione arbitrale di #{hashtag_italia(title)}:"
    lines = [first_line, ""]

    for role in ("ARBITRO", "ASSISTENTI", "IV", "VAR", "AVAR"):
        if roles.get(role):
            lines.append(f"{role}: {roles[role]}")

    return "\n".join(lines), list(prefix_entities)


# =========================
# UEFA - CONTROLLO ORIGINALE
# =========================
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


# =========================
# ITALIA - CONTROLLO CORRETTO
# =========================
def parse_date(text):
    months = {
        "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
        "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
        "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
    }
    value = clean(text).replace("\ufeff", "")

    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", value)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"

    m = re.search(r"\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})\b", value, re.I)
    if m and m.group(2).lower() in months:
        return f"{m.group(3)}-{months[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    return None


def article_publication_date(soup):
    """Trova la data di pubblicazione vicino al titolo dell'articolo, non nelle gare."""
    lines = article_text_lines(soup)
    for line in lines[:12]:
        value = parse_date(line)
        if value:
            return value
    return None


def article_links(soup):
    """Estrae esclusivamente le designazioni Serie A ENILIVE, escludendo le variazioni."""
    result = []
    seen = set()

    for a in soup.find_all("a", href=True):
        label = clean(a.get_text(" ", strip=True)).replace("\ufeff", "")
        href = urljoin(AIA, a["href"])
        lower = href.lower()
        upper_label = label.upper()

        if "/news/" not in lower:
            continue
        if "serie-a-enilive" not in lower:
            continue
        if "designazioni" not in lower:
            continue
        if "variazione" in lower or "VARIAZIONE" in upper_label:
            continue

        # Se il testo del link è disponibile, richiediamo esplicitamente la
        # stringa SERIE A ENILIVE - DESIGNAZIONI.
        if label and not _looks_like_italia_designation_title(label):
            continue

        href = href.split("#", 1)[0]
        if href not in seen:
            result.append(href)
            seen.add(href)

    return result


def aia_category_urls():
    """Genera le pagine dell'archivio Designazioni AIA senza usare il campo ricerca."""
    yield AIA
    for page_number in range(1, AIA_CATEGORY_PAGES):
        yield f"{AIA}&p={page_number}"


def _italia_state_has_url(state, url):
    """Compatibile con il vecchio stato (chiavi data:url) e con il nuovo (url)."""
    bucket = state.setdefault("italia", {})
    if url in bucket:
        return True
    suffix = ":" + url
    return any(key.endswith(suffix) for key in bucket)


def check_italia(state):
    today = datetime.now(ROME).date()
    changed = False
    nuove = 0
    gia_inviate = 0
    controllati = 0

    # La ricerca AIA ?cerca=... restituisce HTTP 400: non viene più chiamata.
    # Usiamo direttamente l'archivio categoria e le sue pagine.
    articoli = []
    seen_urls = set()

    for index_url in aia_category_urls():
        try:
            index_response = request(index_url)
            index = BeautifulSoup(index_response.content, "html.parser")
            links = article_links(index)
            for url in links:
                if url not in seen_urls:
                    articoli.append(url)
                    seen_urls.add(url)
        except Exception as exc:
            print(f"[ITALIA] errore indice AIA {index_url} -> {exc}")

    for url in articoli:
        controllati += 1

        if _italia_state_has_url(state, url):
            gia_inviate += 1
            continue

        try:
            response = request(url)
            soup = BeautifulSoup(response.content, "html.parser")
        except Exception as exc:
            print(f"[ITALIA] errore pagina {url} -> {exc}")
            continue

        publication_date = article_publication_date(soup)
        if publication_date:
            try:
                pub_day = datetime.strptime(publication_date, "%Y-%m-%d").date()
            except ValueError:
                pub_day = None
            if pub_day is not None:
                age_days = (today - pub_day).days
                if age_days > ITALIA_LOOKBACK_DAYS:
                    continue
                if age_days < -2:
                    # Protezione contro date anomale future provenienti da un parsing errato.
                    continue
        else:
            print(f"[ITALIA] data pubblicazione non trovata -> {url}")
            continue

        title, roles = extract_italia_assignment(soup)
        if not title or not roles.get("ARBITRO"):
            continue

        message, entities = format_message_italia(title, roles)

        try:
            send_telegram(message, entities)
        except Exception as exc:
            print(f"[ITALIA] {title}: invio Telegram fallito -> {exc}")
            continue

        # La URL è l'identificativo stabile dell'articolo: evita il problema
        # del vecchio formato data:url quando il workflow viene eseguito in un
        # giorno diverso dalla pubblicazione.
        state["italia"][url] = {
            "date": publication_date,
            "match": title,
            "url": url,
            "sent_at": datetime.now(ROME).isoformat(),
        }
        save_state(state)
        changed = True
        nuove += 1
        print(f"[ITALIA] ✅ inviata: {title}")

    print(f"[ITALIA] {controllati} articoli controllati, {gia_inviate} già inviate, {nuove} nuove")
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
