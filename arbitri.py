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
MAX_ARTICLES = 3  # quante news "designazioni" recenti controllare ad ogni run (Italia)

# =========================
# UEFA
# =========================
UEFA = {
    "Champions League": "https://it.uefa.com/uefachampionsleague/clubs/50139/matches/",
    "Europa League": "https://it.uefa.com/uefaeuropaleague/clubs/50139--juventus/matches/",
    "Conference League": "https://it.uefa.com/uefaconferenceleague/clubs/50139--juventus/matches/",
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

# it.uefa.com è dietro anti-bot: serve un browser reale (Playwright).
PLAYWRIGHT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Etichette realmente usate da it.uefa.com (solo UEFA).
ROLE_PATTERNS = [
    ("ARBITRO", "arbitro"),
    ("ASSISTENTI", "assistenti arbitrali"),
    ("AVAR", "assistente video assistant referee"),
    ("VAR", "video assistant referee"),
    ("IV", "quarto uomo"),
]

ROLE_ORDER = ("ARBITRO", "ASSISTENTI", "IV", "VAR", "AVAR")


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _utf16_len(s: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


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
    """Carica url con Playwright e ritorna l'HTML renderizzato (solo UEFA)."""
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
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def send_telegram(text, entities=None):
    """Testo semplice + entities (emoji custom, grassetto). Niente parse_mode."""
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
# EMOJI CUSTOM
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


# =========================
# UEFA (Playwright, emoji custom, hashtag casa/trasferta)
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


def split_officials(text):
    return re.sub(r"([A-Z]{3})\s+(?=[A-Z][a-z])", r"\1 - ", text or "")


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


def _camel(name):
    name = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ\s]", "", name)
    return "".join(w[:1].upper() + w[1:].lower() for w in name.split())


def hashtag(title):
    # Juve in casa -> JuveCelta, Juve in trasferta -> CeltaJuve.
    parts = [clean(p) for p in re.split(r"\s+(?:vs\.?|v|-|–|—)\s+", title, maxsplit=1, flags=re.I)]
    if len(parts) != 2:
        opponent = _camel(re.sub(r"juventus", "", title, flags=re.I))
        return f"Juve{opponent}" if opponent else "Juve"
    home, away = parts
    juve_home = "juventus" in home.lower()
    opponent = _camel(away if juve_home else home)
    if not opponent:
        return "Juve"
    return f"Juve{opponent}" if juve_home else f"{opponent}Juve"


def format_message_uefa(prefix_data: tuple, title: str, roles: dict) -> tuple[str, list[dict]]:
    prefix_text, prefix_entities = prefix_data
    first_line = f"{prefix_text} Designazione arbitrale di #{hashtag(title)}:"
    lines = [first_line, ""]
    base_offset = _utf16_len(first_line + "\n" + "\n")
    all_entities: list[dict] = list(prefix_entities)

    for role in ROLE_ORDER:
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
                    message, entities = format_message_uefa(PREFIX_UEFA, title, roles)
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
# ITALIA (AIA): ogni partita è un blocco di <p>
#
#   <p><strong>LECCE – JUVENTUS</strong><em>Domenica 25/10 h. 18.00</em></p>
#   <p>MARESCA</p>                      <- arbitro (senza etichetta)
#   <p>MONDIN - BARONE</p>              <- assistenti (senza etichetta)
#   <p>IV: FELICIANI</p>
#   <p>VAR: PATERNA</p>
#   <p>AVAR: BARONI</p>
# =========================
TIME_LIKE = re.compile(r"(^h\.?\s*\d)|(\b\d{1,2}[.:]\d{2}\b)", re.I)


def valid_roles(roles):
    """True solo se c'è almeno l'arbitro e nessun valore sembra un orario/data."""
    if not roles.get("ARBITRO"):
        return False
    for value in roles.values():
        if TIME_LIKE.search(value) or re.search(r"\d{1,2}/\d{1,2}", value):
            return False
    return True


HEADER_RE = re.compile(
    r"^(?P<home>.+?)\s+[–—-]\s+(?P<away>.+?)\s+"
    r"(?:(?P<day>[A-Za-zÀ-ÿ]+)\s+(?P<date>\d{1,2}/\d{1,2})\s+)?"  # giorno/data facoltativi
    r"h\.?\s*(?P<time>\d{1,2}[.:]\d{2})$",
    re.I,
)
LABEL_RE = re.compile(r"^(IV|VAR|AVAR)\s*:\s*(.+)$", re.I)

SHORT_TEAM = {"JUVENTUS": "Juve"}


def person(s):
    s = re.sub(r"\(.*?\)", "", s)  # toglie "(foto)"
    return clean(s).title()


def assistants(s):
    parts = [person(x) for x in re.split(r"\s+[–—-]\s+", s) if x.strip()]
    return " - ".join(parts)


def tag_name(team):
    team = clean(team)
    if team.upper() in SHORT_TEAM:
        return SHORT_TEAM[team.upper()]
    return re.sub(r"[^A-Za-z0-9À-ÿ]", "", team.title())


def format_message_italia(changed, tag, roles):
    """Emoji custom nel prefisso + nomi in grassetto, tramite entities."""
    prefix_text, prefix_entities = PREFIX_ITALIA
    head = "🔄 " if changed else ""  # 🔄 = designazione cambiata
    shift = _utf16_len(head)

    first_line = f"{head}{prefix_text} Designazione arbitrale di #{tag}:"
    lines = [first_line, ""]
    entities = []
    for ent in prefix_entities:
        e = dict(ent)
        e["offset"] += shift
        entities.append(e)

    base_offset = _utf16_len(first_line + "\n" + "\n")
    for role in ROLE_ORDER:
        value = roles.get(role)
        if not value:
            continue
        label = f"{role}: "
        line = label + value
        entities.append({
            "offset": base_offset + _utf16_len(label),
            "length": _utf16_len(value),
            "type": "bold",
        })
        lines.append(line)
        base_offset += _utf16_len(line + "\n")

    return "\n".join(lines), entities


def leaf_paragraphs(root):
    # il sito annida <p> dentro <p>: teniamo solo quelli "foglia"
    return [p for p in root.find_all("p") if not p.find("p")]


def parse_aia_matches(soup):
    root = soup.select_one("div.overflow-hidden") or soup
    matches = []
    current = None
    for p in leaf_paragraphs(root):
        line = clean(p.get_text(" ", strip=True))
        if not line:
            continue
        m = HEADER_RE.match(line)
        if m:
            current = {
                "home": clean(m.group("home")),
                "away": clean(m.group("away")),
                "date": m.group("date") or "",
                "time": m.group("time"),
                "roles": {},
            }
            matches.append(current)
            continue
        if current is None:
            continue
        roles = current["roles"]
        lab = LABEL_RE.match(line)
        if lab:
            roles[lab.group(1).upper()] = person(lab.group(2))
        elif "ARBITRO" not in roles:
            roles["ARBITRO"] = person(line)
        elif "ASSISTENTI" not in roles:
            roles["ASSISTENTI"] = assistants(line)
    return matches


def aia_article_links(soup):
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        if "/news/" in href and "designazioni" in href and "giornata" in href:
            links.append(urljoin(AIA, a["href"]))
    return list(dict.fromkeys(links))[:MAX_ARTICLES]


def article_id(url):
    m = re.search(r"-(\d+)/?$", url.split("?", 1)[0])
    return m.group(1) if m else url


def check_italia(state):
    try:
        index = BeautifulSoup(request(AIA).text, "html.parser")
    except Exception as exc:
        print(f"[ITALIA] AIA: {exc}")
        return False

    links = aia_article_links(index)
    print(f"[ITALIA] {len(links)} articoli designazioni da controllare")
    changed = False

    for url in links:
        try:
            page = BeautifulSoup(request(url).text, "html.parser")
        except Exception as exc:
            print(f"[ITALIA] {url}: {exc}")
            continue

        for m in parse_aia_matches(page):
            if "JUVENTUS" not in (m["home"].upper(), m["away"].upper()):
                continue

            roles = m["roles"]
            label = f"{m['home']} - {m['away']}"
            if not valid_roles(roles):
                print(f"[ITALIA] {label}: ruoli non validi, salto -> {roles}")
                continue

            key = f"{article_id(url)}:{m['home'].upper()}-{m['away'].upper()}"
            prev = state["italia"].get(key)
            if prev and prev.get("roles") == roles:
                print(f"[SKIP] {key}")
                continue

            tag = tag_name(m["home"]) + tag_name(m["away"])
            message, entities = format_message_italia(bool(prev), tag, roles)
            try:
                send_telegram(message, entities)
            except Exception as exc:
                print(f"[ITALIA] Telegram: {exc}")
                continue

            state["italia"][key] = {
                "match": label,
                "date": m["date"],
                "time": m["time"],
                "roles": roles,
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
