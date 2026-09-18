# Sorare MLB — automatické sestavy a přehledy

Webová aplikace pro Sorare: přehled MLB i fotbalu na jednom místě a automat,
který skládá sestavy pro **Hot Streaks** a **Challenger**.

## Stránky

| Cesta | Obsah |
|---|---|
| `/` | Dashboard: nejbližší uzávěrky, co potřebuje pozornost, sbírky, stav automatu, poslední sestavy |
| `/vyhry` | Peníze, Essence, karty a coiny: po soutěžích, gameweecích, kartách a hráčích |
| `/bilance` | Vloženo, vybráno, utraceno za karty, prodeje, poplatky; po letech a měsících; ruční záznamy |
| `/mlb`, `/fotbal` | Sbírka: rarity, pozice, forma (L5/L15, trend), karty bez zápasu, otevřené turnaje, filtr karet |
| `/mlb/sestavy` | Builder sestav MLB: projekce, rozsah floor–ceiling, zápasy v GW, záměna karet, kdy poběží automat |
| `/mlb/kalibrace` | Projekce vs. skutečné body po skončení gameweeku (MAE, bias, graf) |
| `/fotbal/sestavy` | Placeholder: plánovaný formát, otevřené turnaje, co zbývá |
| `/mlb/historie`, `/fotbal/historie` | Vývoj sbírky, sestavy ze Sorare, běhy automatu |
| `/nastaveni` | Přihlášení, kontrola env proměnných, sledované turnaje |

## Struktura kódu

```
sorare_mlb/
  sports/        SportAdapter + BaseballAdapter, FootballAdapter
  overview.py    data pro přehledy (každý blok selhává samostatně)
  history.py     historie v Redisu (běhy automatu, denní snímky sbírky)
  web/           layout (design tokeny) a jednotlivé stránky
  ui.py          registr stránek
tools/schema.py  lokální prohlížení veřejného GraphQL schématu Sorare
```

### Historie

Sorare API vrací jen probíhající a nedávné sestavy. Aplikace si proto ukládá
vlastní historii: záznam o každém dokončeném běhu automatu (max. 200)
a jednou denně snímek sbírky při otevření přehledu (max. 400 na sport).

### Výhry, Essence a trezor

Aplikace si jednou denně stáhne veřejné schéma Sorare (`sorare_mlb/schema.py`)
a dotazy na výhry a příznak trezoru poskládá jen z polí, která v něm opravdu
jsou (`sorare_mlb/features.py`). Co našla, ukazuje `/nastaveni`.

Výhry se berou z `rewardedRankings` a ukládají do archivu v Redisu
(max. 500 umístění na sport). Essence s uvedeným hráčem se přiřadí přesně,
zbytek odměny sestavy se rozpočítá mezi karty podle podílu na bodech.
Sorare u `rewardedRankings` nevrací všechno, např. odměny za streaky
(issue #670 v sorare/api).

Karty v trezoru se nepočítají do formy ani „leží ladem“ a automat je nepoužívá.

### Celá historie účtu

Výhry i platby se ukládají do archivu po čtvrtletích (bez limitu).
Tlačítko „Stáhnout celou historii“ stahuje po dávkách (každá do 40 s,
kvůli limitu Vercelu) a navazuje uloženým kurzorem, dokud nedojde na začátek účtu.

### Bilance

Zdroj plateb se hledá ve schématu (`features._ledger`), záznamy se třídí podle
textu typu (`ledger.RULES`). Ukázku surových dat a přehled, jak se který typ
zatřídil, vrací `/api/ledger/debug`. Co API nevrátí, se doplní ručně.

### Heslo

Aplikace ukazuje peníze. Nastav `APP_PASSWORD` a prohlížeč si heslo vyžádá
(jméno libovolné). `/api/cron` a `/api/continue` mají vlastní secret.

### Nejisté části schématu

Body a pořadí u nedávných sestav (`so5Rankings`) nejsou ověřené proti
schématu. Klient zkouší dotaz ve třech variantách a použije první, kterou
Sorare přijme; UI pak body buď ukáže, nebo napíše, že nejsou k dispozici.
Ověřit se to dá lokálně:

```
python tools/schema.py So5Lineup
python tools/schema.py --search ranking
```

### Fotbalové sestavy (později)

Doplň `build_lineups`, `validate_lineups` a `submit_lineups`
ve `sorare_mlb/sports/football.py` a zapni `sports.football.lineups_enabled`
v `config.yaml`.

---

## Automat sestav MLB
Spustíš ji tlačítkem na stránce, nebo si ji necháš běžet automaticky před
deadlinem gameweeku. Výsledek ti přijde na Discord/Telegram.

Běží na Vercelu (free plán), stav drží v Upstash Redis (taky free).

---

## Jak to funguje

```
Tlačítko na webu  ─┐
GitHub Actions cron ─┼──► /api/cron ──► pipeline ──► Sorare submitLineup
Vercel cron        ─┘                      │
                                           └──► Discord / Telegram
```

Pipeline je stavový automat:

```
QUEUED → CARDS → SCORES → MLB → OPTIMIZE → VALIDATE → SUBMIT → DONE
                                              ↓
                                        NEEDS_REVIEW   (blokující nález
                                                        nebo režim „jen návrh")
```

Vercel Hobby ukončí funkci po 60 s, což na celý průchod nestačí. Každý krok
proto pracuje jen do vyčerpání rozpočtu (výchozí 40 s), uloží stav do Redisu
a zavolá sám sebe znovu. Web zatím jen pollinguje `/api/status`.

---

## Nasazení

### 1. Sorare

Vygeneruj si **nový** API klíč (ten předchozí, pokud jsi ho někde vystavil,
nejdřív zruš).

**2FA je podporované.** Sorare JWT platí 30 dní, takže se jednou za měsíc
přihlásíš na `/login`: klikneš na tlačítko, opíšeš kód z autentikátoru a hotovo.
Všechno ostatní včetně automatického odesílání pak jede samo. Heslo se zadává
jen do env proměnných, formulářem neputuje.

Když token vyprší, cron ti pošle notifikaci místo tichého selhání. Týden předem
navíc dostaneš připomínku.

### 2. Redis

V Vercel dashboardu → Storage → Marketplace → **Upstash Redis**, vytvoř
databázi a připoj ji k projektu. Vercel ti sám doplní `KV_REST_API_URL`
a `KV_REST_API_TOKEN`.

> Bez Redisu aplikace spadne na lokální soubor, který na Vercelu mezi
> invokacemi zmizí — pipeline se pak nikdy nedokončí. `/api/health` tě na to
> upozorní.

### 3. Deploy

```bash
git init && git add . && git commit -m "init"
git remote add origin git@github.com:<ty>/SorareMLB.git
git push -u origin main
```

Na Vercelu naimportuj repozitář. Framework preset nech na **Other** — Python
entrypoint `api/index.py` si najde sám podle `vercel.json`.

### 4. Proměnné prostředí

V Vercel → Settings → Environment Variables:

| Proměnná | Povinná | Poznámka |
|---|---|---|
| `SORARE_API_KEY` | ano | zvyšuje rate limit |
| `SORARE_EMAIL` | ano | |
| `SORARE_PASSWORD` | ano | hashuje se lokálně, na server jde jen hash |
| `SORARE_JWT_AUD` | ano | libovolný string, ale **neměň ho** po prvním přihlášení |
| `SORARE_TOTP_SECRET` | ne | plně automatické přihlášení — viz varování níže |
| `INTERNAL_SECRET` | ano | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `APP_BASE_URL` | doporučeno | `https://tvuj-projekt.vercel.app` |
| `KV_REST_API_URL` / `_TOKEN` | ano | doplní Upstash integrace |
| `DISCORD_WEBHOOK_URL` | volitelné | nebo `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` |
| `CRON_MODE` | volitelné | `auto` (odešle) nebo `propose` (jen navrhne) |

Po deployi otevři `/api/health` — vypíše, co chybí.

### 5. Ověření schématu API

Introspekce (`__schema`) je pro API klíče **zakázaná**, takže se schéma ověřuje
proti veřejnému SDL:

```
/api/schema?secret=XXX&type=Query
/api/schema?secret=XXX&type=createOrUpdateSo5LineupInput
/api/schema?secret=XXX&type=So5Leaderboard&grep=cutoff
```

Endpoint stáhne https://api.sorare.com/graphql/schema a vrátí definici jednoho
typu. Když Sorare něco přejmenuje, tohle ti ukáže aktuální tvar a opravíš
`sorare_mlb/queries.py`.

### 6. Spouštění podle uzávěrky

Automat se řídí skutečnou `cutOffDate` ze Sorare API, ne pevnými časy.
`.github/workflows/trigger.yml` volá každých 15 minut (15–23 UTC) `/api/tick`
a aplikace rozhodne podle `config.yaml → schedule`:

- **build** (60 min před uzávěrkou): postaví sestavy, podle `CRON_MODE`
  je odešle nebo pošle ke schválení,
- **recheck** (20 min před uzávěrkou, jen `CRON_MODE=auto`): postaví je
  znovu s čerstvými startéry a přepíše. Přepis funguje, jen když mutace ve
  schématu bere ID sestavy — `/nastaveni` ukáže, jestli ano.

Každá fáze proběhne pro gameweek jednou. V GitHubu → Settings → Secrets
přidej `APP_BASE_URL` a `INTERNAL_SECRET`. Vercel cron (12:00 UTC) je záloha:
udělá jeden tik a vyhodnotí kalibraci.

---

## Používání

Poprvé (a pak jednou za 30 dní) jdi na `/login` a přihlas se kódem
z autentikátoru. Na hlavní stránce pak:
- **Navrhnout sestavy** — projde pipeline a zastaví se před odesláním
- **Navrhnout a odeslat** — projde to celé včetně submitu
- U návrhu se pak objeví tlačítko pro potvrzení

Automatický běh se řídí `CRON_MODE`.

---

## Bezpečnostní pojistka

Na oficiální MLB lineupy se spolehnout nedá — zveřejňují se pozdě a u ranních
běhů vůbec. Blokující kritéria jsou proto ta, která jsou dostupná vždycky:

- hráč je na **IL** nebo restricted listu,
- jeho **tým v tomhle gameweeku nehraje**,
- **nenastoupil do zápasu déle než 7 dní** (`safety.max_days_without_game`) —
  typicky sedí na lavičce nebo je na farmě

Datum posledního zápasu se bere ze stejné odpovědi Sorare API jako skóre, takže
filtr nestojí ani jedno volání navíc.

Když na všechny požadované sestavy nestačí karty, ubírají se od nejnižší
priority — Hot Streaks tedy nikdy nepadne kvůli tomu, že nevyjde třetí
Challenger sestava, a naopak.

Když validace najde blokující problém, **sestavy se neodešlou** ani v režimu
`auto` — dostaneš notifikaci s návrhem náhrady. Oficiální lineup se použije,
když náhodou k dispozici je, ale jen jako varování, nikdy jako důvod běh
zablokovat.

### K TOTP secretu

`SORARE_TOTP_SECRET` zařídí, že se přihlášení obnovuje samo a na `/login` už
nemusíš. Cena za to je, že oba faktory autentizace leží na stejném serveru —
kdo se dostane k proměnným, dostane se k účtu. Ruční kód jednou za měsíc je
levná pojistka; nech to vypnuté, dokud ti to nezačne vadit.

---

## Lokální vývoj

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env    # vyplň
uvicorn api.index:app --reload --port 8000
```

Bez `KV_REST_API_*` se použije `.local-store.json`, takže Redis lokálně
nepotřebuješ.

```bash
pytest -q     # optimalizátor, pipeline, scheduler, kalibrace, 2FA — žádná síť
```

---

## Co hlídat

1. **Token platí 30 dní.** Hlavní stránka i cron ti připomenou obnovu týden
   předem. Když to prošvihneš, sestavy se neodešlou.
2. **Schéma Sorare API se mění.** `/api/schema?type=...` po každém delším výpadku.
3. **Mapování pozic** v `config.yaml` → `lineup.slots`. Pozice jsou VELKÝMI
   písmeny a s prefixem sportu (`BASEBALL_STARTING_PITCHER`). Ověřeno proti
   Sorare: CI = 1B/3B/DH, MI = 2B/SS/**C**. Špatné mapování projde
   optimalizátorem a spadne až při odeslání.
4. **Párování jmen** Sorare ↔ MLB jede přes normalizované jméno. Duplicity
   (Luis García) doplň do `mlb.manual_player_map`.
5. **Projekce nejsou edge.** Forma × počet zápasů v GW + matchup + Sorare
   projekce + bonus karty. Jestli to funguje, ukáže `/mlb/kalibrace`.
6. **Auto-submit je tvoje odpovědnost.** Pipeline se snaží nezkazit gameweek,
   ale sleduj notifikace — hlavně první dva týdny.
7. **Nikdy necommituj `.env`.** Je v `.gitignore`, ale ověř si to.

## Struktura

```
api/index.py             FastAPI app, všechny endpointy
sorare_mlb/
  runner.py              stavový automat pipeline
  store.py               Redis/lokální úložiště
  client.py              Sorare GraphQL klient
  auth.py                dvoufázové přihlášení s OTP, správa JWT
  queries.py             GraphQL dotazy — jediné místo k opravě při změně API
                         (MLB jede přes So5 s argumentem sport: BASEBALL)
  mlb.py                 MLB StatsAPI
  projections.py         výpočet projekcí
  optimizer.py           ILP přes všechny turnaje (mean ∓ λ·σ, šance na práh)
  scheduler.py           rozhodování podle uzávěrky (/api/tick)
  calibration.py         projekce vs. skutečnost po GW
  validator.py           kontrola před deadlinem
  notify.py              Discord / Telegram
  ui.py                  hlavní stránka
  login_ui.py            přihlašovací stránka (2FA)
config.yaml              pravidla formátu, váhy, bezpečnostní limity
tests/                   optimalizátor + celá pipeline, bez sítě
```

## Licence

MIT
