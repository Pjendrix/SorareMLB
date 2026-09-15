# Sorare MLB — automatické sestavy

Webová aplikace, která skládá sestavy pro **Hot Streaks** a **Challenger**.
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

### 6. Přesný cron přes GitHub Actions

Vercel Hobby cron umí **jen jednou denně** a čas negarantuje — může se opozdit
i o hodinu. Pro běh navázaný na deadline to nestačí, proto je v repozitáři
`.github/workflows/trigger.yml`.

V GitHubu → Settings → Secrets and variables → Actions přidej:
- `APP_BASE_URL`
- `INTERNAL_SECRET`

Pak si v tom souboru uprav časy. Výchozí jsou pondělí a pátek 21:40 UTC, což
odpovídá zhruba 20 minutám před typickým prvním zápasem gameweeku. **Zkontroluj
si to proti reálným deadlinům svého fixture** — cron nezná rozpis MLB.

Vercel cron v `vercel.json` zůstává jako záloha na 14:00 UTC.

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
pytest -q     # 47 testů (optimalizátor, pipeline, 2FA), žádná síť
```

---

## Co hlídat

1. **Token platí 30 dní.** Hlavní stránka i cron ti připomenou obnovu týden
   předem. Když to prošvihneš, sestavy se neodešlou.
2. **Schéma Sorare API se mění.** `/api/schema?type=...` po každém delším výpadku.
3. **Mapování pozic** v `config.yaml` → `lineup.slots`. Pozice jsou VELKÝMI
   písmeny (`STARTING_PITCHER`, `FIRST_BASE`, …). Catchera řadím pod CI —
   ověř si, že to Sorare dělá taky. Špatné mapování = prázdný slot a spadlý job.
4. **Párování jmen** Sorare ↔ MLB jede přes normalizované jméno. Duplicity
   (Luis García) doplň do `mlb.manual_player_map`.
5. **Projekce nejsou edge.** Bez placeného zdroje jde o formu + matchup.
   Je to lepší než náhoda, ale nečekej zázraky.
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
  optimizer.py           ILP přes všechny turnaje
  validator.py           kontrola před deadlinem
  notify.py              Discord / Telegram
  ui.py                  hlavní stránka
  login_ui.py            přihlašovací stránka (2FA)
config.yaml              pravidla formátu, váhy, bezpečnostní limity
tests/                   optimalizátor + celá pipeline, bez sítě
```

## Licence

MIT
