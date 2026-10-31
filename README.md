# Dressing

Houd je kleerkast bij: neem een foto van een kledingstuk, de AI (Claude van Anthropic of ChatGPT van OpenAI) ziet wat het is, welke kleur,
stof en stijl, en jij zegt waar het hangt. Daarna stelt de stylist outfits voor uit je eigen dressing voor een
gelegenheid, het weer of een stemming, zoals de sommelier van de Wijnkelder dat doet met je wijnen.

De app staat in [`web/`](web/): een eigen server (FastAPI + SQLite) met een installeerbare webapp, in Docker, bereikbaar
via Tailscale. Hij is gebouwd op dezelfde basis als de Wijnkelder (CellarPeaks).

## Functies

- **Kledingstuk toevoegen met een foto**: de AI vult naam, soort, kleuren, patroon, materiaal, pasvorm, seizoenen,
  warmte (1-5), formaliteit (1-5), gelegenheden en stijl in. Een merk enkel als het op een label of logo te lezen is.
  Je kijkt het na en vult **waar het hangt** in (de vorige plaats staat al klaar, en eerdere plaatsen worden
  voorgesteld). Zonder sleutel of zonder foto kan je alles ook zelf invullen.
- **Uit een bestelmail** (Zara, H&M, Zalando, …): plak de bevestiging of verzendmail van een online bestelling. De AI
  haalt er elk kledingstuk uit, met naam, kleur, maat, prijs, besteldatum en de productfoto uit de mail (die haalt de
  server op, enkel van adressen op het internet). Je vinkt aan wat je houdt, past soort of naam aan en zegt waar het
  hangt (één plaats voor alles of per stuk). Het kan ook met de mail als **PDF**, of met tot 6 **screenshots** van de
  mail of van je bestelling in de app of op de website van de winkel: dan knipt de AI de foto van elk stuk uit de
  screenshot. Wat al in je dressing lijkt te hangen, staat uitgevinkt.
- **Dressing**: alle stukken als raster met foto, zoeken op naam, kleur, merk of plaats, filteren per soort,
  favorieten of wat er niet is, sorteren op recent, naam, meest of langst niet gedragen.
- **Waar is het?** In de dressing, in de was, bij de stomerij, uitgeleend of opgeborgen (ander seizoen). Enkel wat
  in de dressing hangt, komt in een outfit.
- **Wat aantrekken?** Beschrijf de gelegenheid, het weer of een stemming, of tik een voorstel aan. De stylist stelt 1
  tot 3 complete outfits voor met foto's en de plaats van elk stuk, zegt waarom en geeft een stylingtip. Hij houdt
  rekening met:
  - het **weer** van vandaag en morgen in je woonplaats (Open-Meteo, gratis en zonder sleutel; uit te zetten per vraag),
  - wat je **laatst droeg** (afwisselen, en stukken die lang niet gedragen zijn eens bovenhalen),
  - je **scores en notities** bij eerdere outfits,
  - wat je bij **Instellingen → Over jou** over je stijl schreef,
  - je **stijl uit Inspiratie** (zie hieronder).
  Ontbreekt er iets wezenlijks (een regenjas, nette schoenen), dan zegt hij dat als koopadvies.
  Zijn antwoord wordt nagekeken (een onderstuk en bovenstuk of een kleed, schoenen, niets dubbel, geen outfit dat je al afwees,
  geen twee keer hetzelfde) en zo nodig één keer verbeterd.
- **Valies**: zeg waar en wanneer je op reis gaat en tik aan wat je meeneemt (of laat de stylist helpen inpakken en leg
  een voorgestelde outfit met één tik in de valies). Bij **Wat aantrekken?** kies je dan *Thuis* of de reis: op reis kiest
  de stylist enkel uit je valies, met het weer op je bestemming voor de reisdagen (tot 16 dagen vooruit; verder weg gaat
  hij uit van het gebruikelijke weer daar). Vanaf de dag voor vertrek staat de reis vanzelf aan. Terug thuis maak je de
  valies leeg.
- **Gedragen**: noteer wat je droeg (vanuit een voorstel, bij een stuk of zelf samengesteld), met datum,
  seizoen, temperatuur, gelegenheid, sterren en notitie. De stylist gebruikt die geschiedenis bij volgende voorstellen;
  de temperatuur wordt ingevuld als het lokale weer beschikbaar is en kan je zelf aanpassen.
  Scores en afwijzingen bouwen lokaal een korte voorkeurssamenvatting op. Bij voldoende nieuwe feedback mag de stylist
  daar maximaal één helper-modelaanroep per 30 dagen aan besteden; die ene aanroep heeft een begrensde invoer en uitvoer
  en verschijnt apart in het AI-gebruiksoverzicht.
- **Actuele trends**: de stylist zoekt zelf gericht op het web naar trends voor de vraag (tool `trends_zoeken`: seizoen,
  gelegenheid, jouw stijl en kleuren), met de zoekfunctie van de gekozen AI-dienst en zijn goedkope hulpmodel. Hoogstens één
  zoekopdracht per vraag, en enkel als de vraag over een look, trends of een gelegenheid gaat (een feest, een etentje,
  een dresscode), niet bij een gewone werkdag, het weer alleen of een bijsturing; een onderwerp wordt een week bewaard en gedeeld
  tussen accounts (enkel het onderwerp gaat mee, niets over jou). Een zoekopdracht kost $10 per 1000 plus de tokens en staat
  in het AI-gebruiksoverzicht. De gevonden bronnen staan bij het advies. Daarnaast haalt de app wekelijks trendkoppen op van
  British Vogue en Who What Wear (zonder AI) die mee gaan met elke outfitvraag.
- **Stukken aanvullen**: stukken met een foto die weinig info hebben (vaak ingelezen uit een bestelmail) laat je onder
  Instellingen in één keer opnieuw beschrijven vanaf hun foto. Enkel lege velden worden ingevuld (gelegenheden, stijl,
  pasvorm, materiaal, patroon, beschrijving, ...); naam, categorie, kleur, merk en wat je zelf invulde blijven.
- **Outfit rond dit stuk**: vanuit een kledingstuk meteen een outfit eromheen vragen.
- **Inspiratie**: bewaar outfits die je mooi vindt op Instagram of Pinterest, met tot 6 screenshots tegelijk of de link van
  een pin (de server haalt de foto van de pin op; bij Instagram lukt dat zonder aanmelden niet, daar neem je een screenshot). De
  AI knipt het outfit uit de screenshot, beschrijft het en schrijft uit al je outfits samen een **stijlprofiel** (kleuren,
  silhouetten, sfeer) dat je kan aanpassen. De stylist kiest in die geest, en met **Maak na met mijn kleren** benadert hij een
  bewaard outfit met wat je hebt en zegt hij welk stuk de look zou afmaken. Te vinden bij **Wat aantrekken?** en bij Instellingen.
- **Accounts**: zoals de Wijnkelder. De eerste gebruiker wordt beheerder en maakt andere accounts aan; elk account
  ziet enkel zijn eigen kleren. Een rem op foute wachtwoorden en een log van aanmeldingen.
- **Claude of ChatGPT**, te kiezen per account bij **Instellingen → AI-dienst**. Je kan voor beide een sleutel
  bewaren en wisselen wanneer je wil; de herkenning en de stylist stellen dezelfde vragen aan beide diensten.
  ChatGPT loopt via de Responses API van OpenAI.
- **Bij ChatGPT kiest de stylist zelf het model**: `gpt-6-luna` herkent foto's en stelt gewone outfits samen,
  `gpt-6.1-sol` neemt een dresscode of een look om na te maken (huwelijk, sollicitatie, cocktail, …), en `gpt-6-astra`
  krijgt een tweede kans als een antwoord niet door de controle raakt (een onbekend stuk, geen schoenen). Op Luna kies
  je bij Instellingen *Automatisch* (zo) of *Vast model* (Luna doet alles, Sol en Astra komen er niet aan te pas). Kies je
  een ander model, of *Zuinig*, dan doet dat ene model alles. Sol kost ongeveer 20 keer zoveel als Luna, Astra ongeveer
  100 keer.
- **Aanbevolen modellen** bij *Ander model kiezen* zijn enkel modellen die de live modelcontrole doorstonden (zie
  *Ontwikkelen*): een foto lezen, een functie aanroepen, een reasoning-instelling aannemen en prompt caching melden.
  De andere staan erbij als niet getest.
- **AI-kosten** bij Instellingen: per model (elke oproep één keer, bij het model dat ze beantwoordde) en per analyse.
  ≥ betekent dat een deel niet geprijsd kon worden, ? dat niets bekend is.
- **Toon als beeld**: bij elk voorgesteld outfit kan je met een knop een beeld laten tekenen van iemand die het draagt
  (`gpt-image-2.5-flare`, naar de foto's van je eigen stukken). Enkel als je erop tikt, en enkel met een OpenAI-sleutel;
  het beeld wordt niet bewaard.
- **Eigen API-sleutel per account** (per dienst), versleuteld met het wachtwoord van dat account (AES-GCM, sleutel afgeleid met
  scrypt): niet leesbaar in de database of in back-ups. De beheerder kan terugvallen op `ANTHROPIC_API_KEY` of `OPENAI_API_KEY` uit `.env`.
- **Model** naar keuze bij Instellingen, per dienst (standaard `claude-sonnet-5-5` en `gpt-6-luna`, of `CLAUDE_MODEL` en
  `OPENAI_MODEL` in `.env`). Een foto
  herkennen of een outfit vragen kost meestal minder dan een cent.
- Installeerbaar op het beginscherm (iPhone: Delen → *Zet op beginscherm*), licht en donker thema.
- Elke dag een kopie van de database in `data/backups/` (14 dagen bewaard).

## Installeren in een LXC met Docker (naast Butler)

`web/compose.tailscale.yaml` zet een eigen Tailscale-container naast Dressing (zoals bij Butler). Dressing deelt diens
netwerk, opent **geen poort op de host** en is enkel bereikbaar op `https://dressing.<jouw-tailnet>.ts.net`, met een
geldig certificaat via `tailscale serve` (`web/tailscale/serve.json`). Zo kan hij in dezelfde LXC als Butler draaien:
elke app heeft zijn eigen Tailscale-node, hostnaam en containers (`dressing`, `dressing-tailscale`).

```bash
git clone git@github.com:laurensdehoorne/Dressing.git /opt/dressing
cd /opt/dressing/web
cp .env.example .env            # TS_AUTHKEY invullen (Tailscale-admin → Settings → Keys, met tag:docker)
docker compose -f compose.tailscale.yaml up -d --build
```

- De node krijgt de tag uit `TS_TAG`, standaard **`tag:docker`** (moet in `tagOwners` van je tailnet policy staan).
- HTTPS moet aanstaan in de Tailscale-admin (*DNS → HTTPS Certificates*).
- De login van de node staat in `web/ts-state/`; na de eerste start mag `TS_AUTHKEY` uit `.env`.
- Open de app, maak het beheerdersaccount aan en vul bij **Instellingen** je Anthropic API-sleutel in
  (console.anthropic.com) of OpenAI API-sleutel (platform.openai.com) en je woonplaats voor het weer.

Zonder Tailscale-container kan ook: `docker compose up -d --build` publiceert de app op `127.0.0.1:8081` van de host.

## Bijwerken

```bash
/opt/dressing/web/update.sh
```

Haalt de nieuwste versie op en bouwt enkel opnieuw als er iets veranderd is, met dezelfde opzet als nu draait (met of
zonder Tailscale-container). Via cron, bv. elk kwartier:

```
*/15 * * * * root /opt/dressing/web/update.sh >> /var/log/dressing-update.log 2>&1
```

## Gegevens

Alles staat in `web/data/`: `dressing.db` (SQLite), `photos/` (één JPEG per kledingstuk, max. 1200 px) en
`backups/`. Neem die map mee in je back-up.

## Ontwikkelen

```bash
cd web
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
DATA_DIR=data .venv/bin/uvicorn app.main:app --reload --port 8766
.venv/bin/pytest
```

De tests vervangen de AI en het weer door nepversies; ze kosten niets.

De live modelcontrole stuurt per OpenAI-model twee kleine vragen zoals de app ze stelt, en bewaart het resultaat in
`data/openai_models.json`. Ze kost een fractie van een cent op Luna en enkele centen op Astra:

```bash
cd web
OPENAI_API_KEY=sk-... DATA_DIR=data .venv/bin/python -m app.model_check
```

In de container: `docker compose exec dressing python -m app.model_check` (met `OPENAI_API_KEY` in `.env`).
