# Sorare MLB Lineup Builder

Poloautomatický nástroj pro skládání sestav na Sorare MLB. Navrhne sestavy pro
**Hot Streaks** (priorita 1) a **Challenger** (priorita 2) jedním společným
optimalizačním modelem, ukáže je k odsouhlasení a teprve po potvrzení je odešle.

> **Režim:** návrh → potvrzení → odeslání. Nic se neodesílá bez tvého explicitního `yes`.

---

## Co to dělá

1. Stáhne tvoje karty ze Sorare (`currentUser.baseballCards`) a nadcházející fixtures/turnaje.
2. Stáhne reálný MLB rozpis, probable pitchery, zranění a lineupy z **MLB StatsAPI** (zdarma, bez klíče).
3. Spočítá projekci bodů pro každou kartu (Sorare L15/L5 + MLB StatsAPI korekce).
4. Jedním ILP modelem rozdělí karty mezi turnaje tak, aby se maximalizoval vážený užitek
   (Hot Streaks má vyšší váhu než Challenger) — karta může být jen v jedné sestavě.
5. Před deadlinem umí sestavy revalidovat (`check`) a nahradit hráče, kteří nejsou v oficiálním lineupu.

## Proč ILP a ne "nejdřív Hot Streaks, pak zbytek"

Greedy postup (vezmi top karty do HS, zbytek do Challengeru) je systematicky horší:
karta, která je v HS jen o chlup lepší než alternativa, může v Challengeru chybět
kriticky. ILP řeší přiřazení karta×turnaj globálně s tvrdými omezeními na pozice
a unikátnost karty. Viz `src/sorare_mlb/optimizer.py`.

---

## Instalace

```bash
git clone <tvoje-repo>
cd sorare-mlb-lineups
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"      # nebo: pip install -r requirements.txt
cp .env.example .env    # a vyplň
```

## Přihlášení

Dvě cesty, obě uloží JWT do `.tokens.json` (v `.gitignore`):

```bash
# A) e-mail + heslo (heslo se hashuje bcryptem lokálně, na server jde jen hash)
python -m sorare_mlb login --method password

# B) OAuth "Login with Sorare" — otevře prohlížeč, callback na localhost
python -m sorare_mlb login --method oauth
```

Pro OAuth musíš mít v Sorare developer nastavení jako redirect URI přesně
`http://localhost:8731/callback` (port lze změnit v `.env`).

## Použití

```bash
python -m sorare_mlb probe                 # ověří schéma API proti tvým dotazům (spusť první!)
python -m sorare_mlb cards                 # vypíše portfolio a cache ho
python -m sorare_mlb build                 # navrhne sestavy, uloží do out/lineups-<gw>.json
python -m sorare_mlb build --explain       # + rozpad projekce hráče po hráči
python -m sorare_mlb submit out/lineups-42.json   # zeptá se a teprve pak odešle
python -m sorare_mlb check out/lineups-42.json    # kontrola lineupů před deadlinem
```

---

## Konfigurace (`config.yaml`)

Nejdůležitější části:

- `positions` — mapování Sorare `activePositions` na sloty CI/MI/OF/EH/Flex.
  **Ověř si to** `probe`em; Sorare občas mění názvy a zařazení catchera.
- `weights.hot_streaks` / `weights.challenger` — relativní priorita turnajů v ILP.
- `stack.bonus` — bonus za každého dalšího pálkaře ze stejného MLB týmu (stack).
- `safety.min_projected_floor` — karta s nižším floorem se do Hot Streaks nepustí.
- `projection` — váhy jednotlivých vstupů (L15, L5, matchup, ballpark, K%).

---

## Známé limity a věci, které musíš hlídat

1. **Schéma Sorare API se mění.** Dotazy v `queries.py` odpovídají stavu k datu
   commitu. `probe` ti řekne, co se rozbilo, ještě než přijdeš o gameweek.
2. **Projekce nejsou magie.** Bez placeného zdroje (THE BAT X, Steamer) jsou
   projekce odvozené z formy + matchupu. Je to lepší než náhoda, není to edge.
3. **Oficiální lineupy MLB chodí pozdě** — u večerních zápasů často 1–2 h před
   startem, někdy méně. `check` proto spouštěj opakovaně, ne jednou 30 min předem.
4. **Lock je per gameweek**, ne per zápas. Po locku už `submit` neprojde.
5. **Rate limit.** Bez API klíče narazíš rychle. Klient má backoff + cache portfolia
   v SQLite (`.cache.sqlite`), aby se karty netahaly při každém běhu.
6. **Nikdy necommituj `.env` ani `.tokens.json`.** Jsou v `.gitignore`, ale zkontroluj si to.

## Struktura

```
src/sorare_mlb/
  auth.py         přihlášení (password / OAuth), správa JWT
  client.py       GraphQL klient, rate limit, retry, cache
  queries.py      všechny GraphQL dotazy a mutace na jednom místě
  mlb.py          MLB StatsAPI — rozpis, probables, lineupy, zranění
  models.py       datové typy (Card, Player, Game, Lineup, Tournament)
  projections.py  výpočet projekce a floor/ceiling
  optimizer.py    ILP model přes všechny turnaje
  validator.py    kontrola sestav před deadlinem
  cli.py          příkazová řádka
```

## Licence

MIT
