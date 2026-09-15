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
