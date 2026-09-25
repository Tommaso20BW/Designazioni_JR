import json
import os
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
HISTORY_FILE = DATA_DIR / "designazioni.json"
ITALY_MATCHES_FILE = DATA_DIR / "partite_italia.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

UEFA_URL = (
    "https://it.uefa.com/uefaeuropaleague/match/"
    "2050066--juventus-vs-n-e-c/matchinfo/"
)


ROLE_ORDER = [
    "ARBITRO",
    "ASSISTENTI",
    "IV",
    "VAR",
    "AVAR",
]


COUNTRY_FLAGS = {
    "ALB": "🇦🇱",
    "AND": "🇦🇩",
    "ARM": "🇦🇲",
    "AUT": "🇦🇹",
    "AZE": "🇦🇿",
    "BEL": "🇧🇪",
    "BIH": "🇧🇦",
    "BLR": "🇧🇾",
    "BUL": "🇧🇬",
    "CRO": "🇭🇷",
    "CYP": "🇨🇾",
    "CZE": "🇨🇿",
    "DEN": "🇩🇰",
    "ENG": "🏴",
    "ESP": "🇪🇸",
    "EST": "🇪🇪",
    "FIN": "🇫🇮",
    "FRA": "🇫🇷",
    "GEO": "🇬🇪",
    "GER": "🇩🇪",
    "GRE": "🇬🇷",
    "HUN": "🇭🇺",
    "IRL": "🇮🇪",
    "ISL": "🇮🇸",
    "ISR": "🇮🇱",
    "ITA": "🇮🇹",
    "KAZ": "🇰🇿",
    "KOS": "🇽🇰",
    "LAT": "🇱🇻",
    "LTU": "🇱🇹",
    "LUX": "🇱🇺",
    "MKD": "🇲🇰",
    "MLT": "🇲🇹",
    "MNE": "🇲🇪",
    "MDA": "🇲🇩",
    "NED": "🇳🇱",
    "NOR": "🇳🇴",
    "POL": "🇵🇱",
    "POR": "🇵🇹",
    "ROU": "🇷🇴",
    "RUS": "🇷🇺",
    "SCO": "🏴",
    "SRB": "🇷🇸",
    "SVK": "🇸🇰",
    "SVN": "🇸🇮",
    "SUI": "🇨🇭",
    "SWE": "🇸🇪",
    "TUR": "🇹🇷",
    "UKR": "🇺🇦",
    "WAL": "🏴",
}


def load_json(path, default):
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Secret mancanti: configura TELEGRAM_TOKEN e CHAT_ID."
        )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message,
        },
        timeout=30,
    )

    response.raise_for_status()


def fetch_page_requests(url):
    response = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/140 Safari/537.36"
            )
        },
        timeout=30,
    )

    response.raise_for_status()
    return response.text


def fetch_page_playwright(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
        )

        page = browser.new_page(
            locale="it-IT",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/140 Safari/537.36"
            ),
        )

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=30000,
            )
        except Exception:
            pass

        time.sleep(3)

        html = page.content()

        browser.close()

    return html


def clean_text(value):
    if not value:
        return ""

    value = re.sub(r"\s+", " ", value)
    return value.strip()


def clean_uefa_value(value):
    value = clean_text(value)

    # Elimina eventuale testo tecnico successivo al codice nazionale.
    match = re.search(
        r"\b(ALB|AND|ARM|AUT|AZE|BEL|BIH|BLR|BUL|CRO|CYP|CZE|DEN|"
        r"ENG|ESP|EST|FIN|FRA|GEO|GER|GRE|HUN|IRL|ISL|ISR|ITA|"
        r"KAZ|KOS|LAT|LTU|LUX|MKD|MLT|MNE|MDA|NED|NOR|POL|POR|"
        r"ROU|RUS|SCO|SRB|SVK|SVN|SUI|SWE|TUR|UKR|WAL)\b",
        value,
    )

    if match:
        value = value[:match.end()]

    return clean_text(value)


def split_name_country(value):
    value = clean_uefa_value(value)

    match = re.search(
        r"\b(ALB|AND|ARM|AUT|AZE|BEL|BIH|BLR|BUL|CRO|CYP|CZE|DEN|"
        r"ENG|ESP|EST|FIN|FRA|GEO|GER|GRE|HUN|IRL|ISL|ISR|ITA|"
        r"KAZ|KOS|LAT|LTU|LUX|MKD|MLT|MNE|MDA|NED|NOR|POL|POR|"
        r"ROU|RUS|SCO|SRB|SVK|SVN|SUI|SWE|TUR|UKR|WAL)\b",
        value,
    )

    if not match:
        return value, ""

    name = clean_text(value[:match.start()])
    country = match.group(1)

    return name, country


def surname_only(name):
    name = clean_text(name)

    if not name:
        return ""

    parts = name.split()

    # UEFA normalmente restituisce Nome Cognome.
    # Per nomi composti mantiene l'ultimo elemento come cognome.
    return parts[-1]


def format_uefa_person(value):
    name, country = split_name_country(value)

    if not name:
        return ""

    surname = surname_only(name)
    flag = COUNTRY_FLAGS.get(country, "")

    if flag:
        return f"{surname} {flag}"

    return surname


def extract_text_lines(soup):
    text = soup.get_text("\n")

    lines = []

    for line in text.splitlines():
        line = clean_text(line)

        if line:
            lines.append(line)

    return lines


def find_uefa_role_block(soup, role_words):
    """
    Cerca un blocco UEFA specifico evitando di prendere:
    - il titolo della pagina
    - l'intera pagina
    - intestazioni come 'Assistenti arbitrali'
    """

    elements = soup.find_all(
        [
            "div",
            "li",
            "section",
            "article",
            "p",
            "span",
            "dd",
            "dt",
        ]
    )

    candidates = []

    for element in elements:
        text = clean_text(element.get_text(" ", strip=True))

        if not text:
            continue

        lower = text.lower()

        if all(word.lower() in lower for word in role_words):
            candidates.append((len(text), element))

    # Prima i blocchi più piccoli.
    candidates.sort(key=lambda item: item[0])

    for _, element in candidates:
        text = clean_text(element.get_text(" ", strip=True))

        # Evita elementi enormi che contengono praticamente tutta la pagina.
        if len(text) > 500:
            continue

        return element

    return None


def extract_uefa_assistenti(soup):
    """
    Estrae esclusivamente i due assistenti.

    È importante non cercare semplicemente la parola
    'ASSISTENTI', perché UEFA può avere intestazioni come
    'Assistenti arbitrali'. In quel caso il vecchio parser
    restituiva proprio 'arbitrali'.
    """

    role_block = find_uefa_role_block(
        soup,
        ["assistenti"],
    )

    if role_block:
        # Prima prova a prendere i discendenti più piccoli.
        descendants = role_block.find_all(
            ["li", "span", "div", "p", "a"]
        )

        values = []

        for element in descendants:
            text = clean_text(element.get_text(" ", strip=True))

            if not text:
                continue

            # Deve contenere un codice nazionale UEFA.
            if not re.search(
                r"\b(?:"
                + "|".join(COUNTRY_FLAGS.keys())
                + r")\b",
                text,
            ):
                continue

            # Scarta intestazioni.
            if text.lower() in {
                "assistenti",
                "assistenti arbitrali",
                "assistant referees",
            }:
                continue

            values.append(text)

        # Mantieni soltanto valori distinti.
        unique = []

        for value in values:
            value = clean_uefa_value(value)

            if value not in unique:
                unique.append(value)

        # Cerca una coppia reale di assistenti.
        if len(unique) >= 2:
            formatted = [
                format_uefa_person(unique[0]),
                format_uefa_person(unique[1]),
            ]

            formatted = [x for x in formatted if x]

            if len(formatted) >= 2:
                return " – ".join(formatted[:2])

    # Fallback robusto: analizza il testo riga per riga.
    lines = extract_text_lines(soup)

    for index, line in enumerate(lines):
        normalized = line.lower()

        if "assistenti" not in normalized:
            continue

        # Non usare mai direttamente una riga che sia soltanto
        # 'assistenti arbitrali'.
        if normalized in {
            "assistenti",
            "assistenti arbitrali",
            "assistant referees",
        }:
            following = lines[index + 1:index + 8]
        else:
            following = [line] + lines[index + 1:index + 6]

        found = []

        for candidate in following:
            candidate = clean_uefa_value(candidate)

            if candidate.lower() in {
                "assistenti",
                "assistenti arbitrali",
                "assistant referees",
            }:
                continue

            if not re.search(
                r"\b(?:"
                + "|".join(COUNTRY_FLAGS.keys())
                + r")\b",
                candidate,
            ):
                continue

            formatted = format_uefa_person(candidate)

            if formatted and formatted not in found:
                found.append(formatted)

            if len(found) == 2:
                return " – ".join(found)

    return ""


def extract_uefa_roles(html):
    soup = BeautifulSoup(html, "html.parser")

    roles = {}

    # -------------------------
    # ASSISTENTI
    # -------------------------
    assistants = extract_uefa_assistenti(soup)

    if assistants:
        roles["ASSISTENTI"] = assistants

    # -------------------------
    # Altri ruoli
    # -------------------------

    role_patterns = {
        "ARBITRO": [
            ["arbitro"],
            ["referee"],
        ],
        "IV": [
            ["quarto", "ufficiale"],
            ["fourth", "official"],
        ],
        "VAR": [
            ["var"],
        ],
        "AVAR": [
            ["avar"],
            ["assistant", "var"],
        ],
    }

    for role, patterns in role_patterns.items():
        if role == "ASSISTENTI":
            continue

        found_value = ""

        for words in patterns:
            element = find_uefa_role_block(
                soup,
                words,
            )

            if not element:
                continue

            text = clean_text(
                element.get_text(" ", strip=True)
            )

            text = clean_uefa_value(text)

            # Rimuove eventuali etichette iniziali.
            text = re.sub(
                r"^(ARBITRO|REFEREE|VAR|AVAR)\s*:?\s*",
                "",
                text,
                flags=re.IGNORECASE,
            )

            if role == "IV":
                text = re.sub(
                    r"^(quarto\s+ufficiale|fourth\s+official)\s*:?\s*",
                    "",
                    text,
                    flags=re.IGNORECASE,
                )

            # Non accettare blocchi troppo grandi.
            if text and len(text) < 150:
                found_value = text
                break

        if found_value:
            roles[role] = format_uefa_person(found_value)

    # Fallback per eventuali strutture UEFA differenti.
    if not roles.get("ARBITRO") or not roles.get("IV"):
        lines = extract_text_lines(soup)

        for index, line in enumerate(lines):
            normalized = line.lower()

            if not roles.get("ARBITRO"):
                if normalized.startswith("arbitro") or normalized.startswith(
                    "referee"
                ):
                    candidate = line.split(":", 1)[-1]
                    candidate = clean_uefa_value(candidate)

                    formatted = format_uefa_person(candidate)

                    if formatted:
                        roles["ARBITRO"] = formatted

            if not roles.get("IV"):
                if (
                    "quarto ufficiale" in normalized
                    or "fourth official" in normalized
                ):
                    candidate = line.split(":", 1)[-1]
                    candidate = clean_uefa_value(candidate)

                    formatted = format_uefa_person(candidate)

                    if formatted:
                        roles["IV"] = formatted

    return roles


def extract_italy_roles(html):
    soup = BeautifulSoup(html, "html.parser")

    roles = {}

    labels = {
        "ARBITRO": [
            "Arbitro",
            "Arbitro:",
        ],
        "ASSISTENTI": [
            "Assistenti",
            "Assistenti:",
        ],
        "IV": [
            "IV",
            "Quarto Uomo",
            "Quarto ufficiale",
        ],
        "VAR": [
            "VAR",
        ],
        "AVAR": [
            "AVAR",
        ],
    }

    # Prima prova con elementi piccoli.
    for role, possible_labels in labels.items():
        for label in possible_labels:
            elements = soup.find_all(
                string=lambda text: (
                    text
                    and clean_text(str(text)).lower()
                    == label.lower()
                )
            )

            for element in elements:
                parent = element.parent

                if not parent:
                    continue

                parent_text = clean_text(
                    parent.get_text(" ", strip=True)
                )

                if ":" in parent_text:
                    value = parent_text.split(":", 1)[1].strip()

                    if value:
                        roles[role] = value
                        break

                sibling = parent.find_next_sibling()

                if sibling:
                    value = clean_text(
                        sibling.get_text(" ", strip=True)
                    )

                    if value:
                        roles[role] = value
                        break

            if roles.get(role):
                break

    # Fallback: analisi delle righe.
    if len(roles) < 5:
        lines = extract_text_lines(soup)

        for index, line in enumerate(lines):
            normalized = line.lower()

            for role, possible_labels in labels.items():
                if roles.get(role):
                    continue

                matched = False

                for label in possible_labels:
                    if normalized.startswith(label.lower()):
                        matched = True
                        break

                if not matched:
                    continue

                if ":" in line:
                    value = line.split(":", 1)[1].strip()
                elif index + 1 < len(lines):
                    value = lines[index + 1]
                else:
                    value = ""

                value = clean_text(value)

                if value:
                    roles[role] = value

    # Normalizzazione assistenti italiani.
    if roles.get("ASSISTENTI"):
        value = roles["ASSISTENTI"]

        value = re.sub(
            r"^\s*assistenti\s*:?\s*",
            "",
            value,
            flags=re.IGNORECASE,
        )

        value = re.sub(r"\s+", " ", value).strip()

        # Uniforma separatori.
        value = re.sub(
            r"\s*(?:-|–|—|/|\|)\s*",
            " – ",
            value,
        )

        roles["ASSISTENTI"] = value

    return roles


def uefa_match_title(url):
    """
    Ricava il titolo direttamente dallo slug UEFA.
    Non usa <title>, perché UEFA contiene nel titolo anche
    testo tecnico della pagina.
    """

    match = re.search(
        r"/match/\d+--([^/]+)/matchinfo",
        url,
        flags=re.IGNORECASE,
    )

    if not match:
        return "Juventus"

    slug = match.group(1)

    parts = slug.split("-vs-")

    if len(parts) != 2:
        return "Juventus"

    home = parts[0]
    away = parts[1]

    def prettify(value):
        value = value.replace("-", " ").strip()

        if value.lower() == "juventus":
            return "Juventus"

        if value.lower() == "n.e.c.":
            return "N.E.C."

        if value.lower() == "n.e.c":
            return "N.E.C."

        return value.title()

    return f"{prettify(home)} - {prettify(away)}"


def make_hashtag(title):
    """
    Juventus - N.E.C. -> #JuveNEC
    Juventus - Atalanta -> #JuveAtalanta
    """

    parts = re.split(r"\s+-\s+", title)

    if len(parts) != 2:
        return "#Juve"

    home = parts[0].strip()
    away = parts[1].strip()

    if home.lower() == "juventus":
        home_tag = "Juve"
    else:
        home_tag = re.sub(r"[^A-Za-z0-9]", "", home)

    away_tag = re.sub(
        r"[^A-Za-z0-9]",
        "",
        away,
    )

    if away_tag.upper() == "NEC":
        away_tag = "NEC"

    return f"#{home_tag}{away_tag}"


def format_uefa_message(title, roles):
    hashtag = make_hashtag(title)

    lines = [
        f"🇪🇺ℹ️ Designazione arbitrale di {hashtag}:",
        "",
    ]

    for role in ROLE_ORDER:
        value = roles.get(role)

        if value:
            lines.append(
                f"{role}: {value}"
            )

    return "\n".join(lines)


def format_italy_message(title, roles):
    hashtag = make_hashtag(title)

    lines = [
        f"🇮🇹ℹ️ Designazione arbitrale di {hashtag}:",
        "",
    ]

    for role in ROLE_ORDER:
        value = roles.get(role)

        if value:
            lines.append(
                f"{role}: {value}"
            )

    return "\n".join(lines)


def process_uefa(url, history):
    print(f"[UEFA] Controllo: {url}")

    html = fetch_page_playwright(url)

    roles = extract_uefa_roles(html)

    print("[UEFA] Ruoli trovati:")
    for role in ROLE_ORDER:
        print(f"  {role}: {roles.get(role, '')}")

    required = [
        "ARBITRO",
        "ASSISTENTI",
        "IV",
        "VAR",
        "AVAR",
    ]

    missing = [
        role
        for role in required
        if not roles.get(role)
    ]

    if missing:
        print(
            "[UEFA] Designazione incompleta, mancanti: "
            + ", ".join(missing)
        )
        return

    title = uefa_match_title(url)

    message = format_uefa_message(
        title,
        roles,
    )

    key = f"UEFA:{url}"

    old_message = history.get("uefa", {}).get(key)

    if old_message == message:
        print("[UEFA] Nessuna modifica.")
        return

    print("[UEFA] Nuova designazione:")
    print(message)

    send_telegram(message)

    history.setdefault("uefa", {})[key] = message


def process_italy(url, history):
    print(f"[ITALIA] Controllo: {url}")

    html = fetch_page_requests(url)

    roles = extract_italy_roles(html)

    print("[ITALIA] Ruoli trovati:")
    for role in ROLE_ORDER:
        print(f"  {role}: {roles.get(role, '')}")

    required = [
        "ARBITRO",
        "ASSISTENTI",
        "IV",
        "VAR",
        "AVAR",
    ]

    missing = [
        role
        for role in required
        if not roles.get(role)
    ]

    if missing:
        print(
            "[ITALIA] Designazione incompleta, mancanti: "
            + ", ".join(missing)
        )
        return

    title = extract_italy_match_title(
        html,
        url,
    )

    message = format_italy_message(
        title,
        roles,
    )

    key = url

    old_message = history.get("italia", {}).get(key)

    if old_message == message:
        print("[ITALIA] Nessuna modifica.")
        return

    print("[ITALIA] Nuova designazione:")
    print(message)

    send_telegram(message)

    history.setdefault("italia", {})[key] = message


def extract_italy_match_title(html, url):
    soup = BeautifulSoup(html, "html.parser")

    # Prova titoli e heading della partita.
    for selector in [
        "h1",
        "h2",
        "[class*='match']",
        "[class*='title']",
    ]:
        for element in soup.select(selector):
            text = clean_text(
                element.get_text(" ", strip=True)
            )

            if not text:
                continue

            if "juventus" in text.lower():
                # Cerca una partita tipo Juventus - Atalanta.
                match = re.search(
                    r"(Juventus)\s*(?:-|–|—|vs\.?|v)\s*"
                    r"([A-Za-zÀ-ÿ0-9 .'-]+)",
                    text,
                    flags=re.IGNORECASE,
                )

                if match:
                    away = clean_text(match.group(2))

                    away = re.sub(
                        r"\s+(?:Serie A|Coppa Italia|Supercoppa).*",
                        "",
                        away,
                        flags=re.IGNORECASE,
                    )

                    return f"Juventus - {away.strip()}"

    # Fallback dal nome file/URL.
    slug = url.rstrip("/").split("/")[-1]

    slug = re.sub(
        r"\?.*$",
        "",
        slug,
    )

    match = re.search(
        r"juventus[-_](?:vs[-_])?([^/]+)",
        slug,
        flags=re.IGNORECASE,
    )

    if match:
        away = match.group(1)
        away = re.sub(
            r"[-_]",
            " ",
            away,
        )

        return f"Juventus - {away.title()}"

    return "Juventus"


def check_italy(history):
    matches = load_json(
        ITALY_MATCHES_FILE,
        [],
    )

    if not matches:
        print(
            "[ITALIA] Nessuna partita configurata "
            "in data/partite_italia.json."
        )
        return

    for item in matches:
        if isinstance(item, str):
            url = item
        elif isinstance(item, dict):
            url = item.get("url", "")
        else:
            continue

        if not url:
            continue

        try:
            process_italy(
                url,
                history,
            )
        except Exception as exc:
            print(
                f"[ITALIA] Errore su {url}: {exc}"
            )


def main():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Secret mancanti: configura TELEGRAM_TOKEN e CHAT_ID."
        )

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    history = load_json(
        HISTORY_FILE,
        {
            "uefa": {},
            "italia": {},
        },
    )

    # Garantisce la struttura corretta.
    if not isinstance(history, dict):
        history = {
            "uefa": {},
            "italia": {},
        }

    history.setdefault("uefa", {})
    history.setdefault("italia", {})

    # UEFA
    try:
        process_uefa(
            UEFA_URL,
            history,
        )
    except Exception as exc:
        print(
            f"[UEFA] Errore: {exc}"
        )

    # Italia
    try:
        check_italy(history)
    except Exception as exc:
        print(
            f"[ITALIA] Errore generale: {exc}"
        )

    save_json(
        HISTORY_FILE,
        history,
    )

    print("[OK] Controllo completato.")


if __name__ == "__main__":
    main()
