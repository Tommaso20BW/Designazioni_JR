# ============================================================
# PATCH per arbitri.py — sostituisci i blocchi indicati
# ============================================================

# 1) URL corretti (manca "it.uefa.com/" nella Conference League)
UEFA = {
    "Champions League": "https://it.uefa.com/uefachampionsleague/clubs/50139/matches/",
    "Europa League": "https://it.uefa.com/uefaeuropaleague/clubs/50139--juventus/matches/",
    "Conference League": "https://it.uefa.com/uefaeuropaconferenceleague/clubs/50139--juventus/matches/",
}

# 2) Etichette allineate a quelle realmente usate da it.uefa.com
#    (Quarto uomo, non "quarto ufficiale"; VAR/AVAR con dicitura completa
#    e AVAR PRIMA di VAR perché "Video Assistant Referee" è una sottostringa
#    di "Assistente Video Assistant Referee")
ROLE_PATTERNS = [
    ("ARBITRO", "arbitro"),
    ("ASSISTENTI", "assistenti arbitrali"),
    ("AVAR", "assistente video assistant referee"),
    ("VAR", "video assistant referee"),
    ("IV", "quarto uomo"),
]


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
                remaining = remaining[: match.start()] + remaining[match.end() :]

    return result
