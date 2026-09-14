"""Všechny GraphQL dotazy a mutace na jednom místě.

Sorare své schéma průběžně mění a baseballová část je hůř zdokumentovaná než
fotbalová. Proto:

* každý dotaz má v komentáři, co od něj čekáme,
* `probe` v CLI pustí introspekci a řekne ti, která pole už neexistují,
* klient umí u nepodstatných polí degradovat (viz client.py).

Když ti `probe` nahlásí rozdíl, uprav dotaz **jen tady**.
"""

# --------------------------------------------------------------------- portfolio

USER_CARDS = """
query UserBaseballCards($rarities: [Rarity!], $after: String) {
  currentUser {
    slug
    nickname
    baseballCards(rarities: $rarities, first: 50, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        slug
        rarity
        seasonYear
        player: anyPlayer {
          slug
          displayName
          positions
          team: activeClub {
            ... on BaseballTeam { slug name abbreviation }
          }
        }
      }
    }
  }
}
"""

# Posledních N Sorare skóre hráče. Tohle je hlavní vstup projekce.
PLAYER_SCORES = """
query PlayerScores($slugs: [String!]!) {
  baseballPlayers(slugs: $slugs) {
    slug
    displayName
    positions
    lastFifteenSoFiveScore: lastFifteenScore
    gameStats: baseballGameStats(last: 15) {
      nodes {
        score
        game { id startDate }
      }
    }
  }
}
"""

# --------------------------------------------------------------------- fixtures

UPCOMING_FIXTURES = """
query UpcomingBaseballFixtures {
  baseball {
    allFixtures(first: 4) {
      nodes {
        slug
        displayName
        state
        startDate
        endDate
        gameWeek
      }
    }
  }
}
"""

FIXTURE_TOURNAMENTS = """
query FixtureCompetitions($fixtureSlug: String!) {
  baseball {
    fixture(slug: $fixtureSlug) {
      slug
      startDate
      endDate
      competitions {
        nodes {
          slug
          displayName
          rarityType
          lineupsCount
          maxLineups
          submissionDeadline: closesAt
        }
      }
    }
  }
}
"""

MY_LINEUPS = """
query MyLineups($fixtureSlug: String!) {
  currentUser {
    baseballLineups(fixtureSlug: $fixtureSlug) {
      nodes {
        id
        competition { slug displayName }
        cards { slug }
      }
    }
  }
}
"""

# --------------------------------------------------------------------- mutations

# Sorare pojmenovává tuhle mutaci v baseballu jinak než ve fotbale a měnil ji.
# Držíme dvě varianty a klient zkusí druhou, když první neprojde validací schématu.
SUBMIT_LINEUP_PRIMARY = """
mutation CreateBaseballLineup($input: createBaseballLineupInput!) {
  createBaseballLineup(input: $input) {
    lineup { id }
    errors { message path }
  }
}
"""

SUBMIT_LINEUP_FALLBACK = """
mutation SubmitLineup($input: submitLineupInput!) {
  submitLineup(input: $input) {
    lineup { id }
    errors { message path }
  }
}
"""

# --------------------------------------------------------------------- introspection

INTROSPECT_TYPE = """
query IntrospectType($name: String!) {
  __type(name: $name) {
    name
    kind
    fields { name description }
    inputFields { name type { name kind ofType { name kind } } }
  }
}
"""

INTROSPECT_ROOT = """
query IntrospectRoot {
  __schema {
    queryType { fields { name } }
    mutationType { fields { name } }
  }
}
"""
