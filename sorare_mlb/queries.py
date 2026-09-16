"""GraphQL dotazy a mutace pro Sorare MLB.

Ověřeno proti oficiálnímu SDL (https://api.sorare.com/graphql/schema).
Zásadní věci, které se liší od intuice:

* Neexistuje kořen `baseball`. MLB sdílí So5 systém s fotbalem a odlišuje se
  argumentem `sport: BASEBALL`.
* Karty se berou přes `currentUser.cards(sport: BASEBALL)`, ne přes
  `baseballCards` na Query.
* „Turnaje" jsou So5Leaderboardy (`so5.upcomingLeaderboards`), ne competitions.
* Sestava se odesílá mutací `createOrUpdateSo5Lineup`, která chce **ID**
  leaderboardu (ne slug) a pole `So5AppearanceInput` s povinným `captain`.
* Pozice jsou VELKÝMI písmeny: STARTING_PITCHER, FIRST_BASE, …

Introspekce (`__schema`) je pro API klíče zakázaná, takže schéma se ověřuje
endpointem /api/schema, který stahuje veřejné SDL.
"""

# --------------------------------------------------------------------- portfolio

# Skóre tahám rovnou s kartami — ušetří to samostatný dotaz na hráče
# a hlavně nepotřebuju kořenový dotaz na hráče podle slugu.
USER_CARDS = """
query UserBaseballCards($rarities: [Rarity!], $after: String) {
  currentUser {
    slug
    nickname
    cards(sport: BASEBALL, rarities: $rarities, first: 20, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        slug
        rarityTyped
        seasonYear
        inSeasonEligible
        anyPositions
        anyTeam { slug name }
        anyPlayer {
          slug
          displayName
          # Projekce skóre na příští gameweek (není to příznak startu!).
          nextClassicFixtureProjectedScore
          playerGameScores(last: 15) {
            score
            anyGame { date }
          }
        }
      }
    }
  }
}
"""

# --------------------------------------------------------------------- turnaje

# upcomingLeaderboards vrací otevřené leaderboardy napříč sporty, proto se
# filtruje podle so5Fixture.sport na naší straně.
UPCOMING_LEADERBOARDS = """
query UpcomingLeaderboards {
  so5 {
    upcomingLeaderboards {
      id
      slug
      displayName
      rarityType
      cutOffDate
      gameWeek
      mySo5LineupsCount
      requiresManagerTeam
      myManagerTeams { id name }
      so5Fixture {
        slug
        gameWeek
        sport
        startDate
        endDate
      }
    }
  }
}
"""

MY_LINEUPS = """
query MyLineups {
  so5 {
    myOngoingAndRecentSo5Lineups {
      id
      so5Leaderboard { slug displayName }
    }
  }
}
"""

# --------------------------------------------------------------------- mutace

SUBMIT_LINEUP = """
mutation CreateOrUpdateSo5Lineup($input: createOrUpdateSo5LineupInput!) {
  createOrUpdateSo5Lineup(input: $input) {
    so5Lineup { id }
    errors { message path }
  }
}
"""


# --------------------------------------------------------------------- lavička

# Odznak "PP" na kartě = vysoká pravděpodobnost startu. Sorare ji nevystavuje
# jako pole, ale dá se na ni filtrovat — a protože je lavička navázaná na
# konkrétní leaderboard, sedí to na správný gameweek. (`nextGame` na hráči
# vrací nejbližší zápas vůbec, což u vícedenního gameweeku ukazuje jinam.)
# Ohlášení startující nadhazovači pro celý gameweek.
#
# Bere se to ze zápasů fixture, ne z hráče ani z lavičky:
#   * `nextGame` na hráči vrací nejbližší zápas vůbec, klidně mimo gameweek
#     (Yamamoto startoval 16. 9., zatímco gameweek byl 19.–21. 9.),
#   * `myFilteredBench` vrací prázdno bez kontextu skládané sestavy.
# `anyGames` je rozhraní, takže jde použít fragment na GameOfBaseball.
PROBABLE_STARTERS = """
query FixtureProbablePitchers($slug: String!) {
  so5 {
    so5Fixture(slug: $slug) {
      slug
      startDate
      endDate
      anyGames {
        ... on GameOfBaseball {
          id
          date
          probablePitchers {
            slug
            displayName
          }
        }
      }
    }
  }
}
"""

# Diagnostická varianta: filtry se předávají jako proměnná, takže jde
# vyzkoušet, který z nich lavičku vyprázdní.
BENCH_PROBE = """
query BenchProbe($slug: String!, $filters: BenchFilterInput!, $after: String) {
  so5 {
    so5Leaderboard(slug: $slug) {
      slug
      myFilteredBench(filters: $filters, first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          position
          anyPlayer { slug displayName }
        }
      }
    }
  }
}
"""


# --------------------------------------------------------------------- přehledy
#
# Karty libovolného sportu. Stejná pole jako USER_CARDS, jen bez projekce
# (`nextClassicFixtureProjectedScore` je ověřený jen u baseballu).
SPORT_CARDS = """
query UserSportCards($sport: Sport!, $rarities: [Rarity!], $after: String) {
  currentUser {
    cards(sport: $sport, rarities: $rarities, first: 20, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        slug
        rarityTyped
        seasonYear
        anyPositions
        anyTeam { slug name }
        anyPlayer {
          slug
          displayName
          playerGameScores(last: 15) {
            score
            anyGame { date }
          }
        }
      }
    }
  }
}
"""

# Historie sestav. Sorare schéma pro výsledky není ověřené, proto tři úrovně:
# klient zkusí nejbohatší variantu a při chybě schématu spadne na jednodušší.
# Pole leaderboardu a fixture jsou stejná jako v UPCOMING_LEADERBOARDS
# (ověřená), nejistá jsou jen `so5Rankings` a `score`.
RECENT_LINEUPS_TIERS = [
    ("RecentLineupsScored", """
query RecentLineupsScored {
  so5 {
    myOngoingAndRecentSo5Lineups {
      id
      so5Rankings { ranking score }
      so5Leaderboard {
        slug
        displayName
        rarityType
        so5Fixture { slug gameWeek sport startDate endDate }
      }
    }
  }
}
"""),
    ("RecentLineupsFixture", """
query RecentLineupsFixture {
  so5 {
    myOngoingAndRecentSo5Lineups {
      id
      so5Leaderboard {
        slug
        displayName
        rarityType
        so5Fixture { slug gameWeek sport startDate endDate }
      }
    }
  }
}
"""),
    ("RecentLineupsBasic", MY_LINEUPS.replace("query MyLineups", "query RecentLineupsBasic")),
]
