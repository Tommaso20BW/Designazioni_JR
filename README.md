# Arbitri_JR

Bot GitHub Actions per controllare le designazioni arbitrali della Juventus e inviarle su Telegram.

## Competizioni controllate

UEFA:
- Champions League
- Europa League
- Conference League

Italia:
- Serie A
- Coppa Italia
- Supercoppa Italiana

## Avvio

Il workflow è manuale:

Actions → Controlla designazioni arbitrali → Run workflow

## Secret richiesti

Settings → Secrets and variables → Actions → New repository secret

- TELEGRAM_TOKEN
- CHAT_ID

## Storico

data/designazioni.json mantiene le designazioni già inviate.

Una designazione già inviata non viene reinviata.

## Fonti

UEFA:
https://it.uefa.com/uefachampionsleague/clubs/50139/matches/
https://it.uefa.com/uefaeuropaleague/clubs/50139--juventus/matches/
https://it.uefaeuropaconferenceleague/clubs/50139--juventus/matches/

Italia:
https://www.aia-figc.it/news/?c=9
