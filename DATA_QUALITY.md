# Data quality findings and vendor feedback

These issues were observed in the supplied API data, and they affect
the accuracy or completeness of the requested analysis.

## High-priority issues

1. **Identifiers are not consistent across endpoints.** Team IDs differ between
   API versions, while roster records use player slugs instead of the player IDs
   used elsewhere. The pipeline links exact IDs or exact normalized names and
   records the team ID mappings. It does not use fuzzy matching because a wrong
   match would attach statistics or injuries to the wrong person.

2. **Player coverage is incomplete.** The roster returned 18 players, but the
   canonical player endpoint returned 12. One box score also referenced an
   unknown player ID, and one injury could not be matched safely. These records
   are retained as flagged placeholders or unresolved audit records rather than
   being dropped or guessed.

3. **Box-score data is incomplete and sometimes unavailable.** One game returned
   a `Game not found` application error, and available box scores contain only
   two players per team. Their point totals do not equal the team scores. As a
   result, player averages and injury comparisons use only the appearances that
   were successfully returned and should not be treated as complete-season
   results.

4. **Injury timing is not precise enough for causal analysis.** Injury reports
   provide a date but no report time, so games on the same date cannot be placed
   safely before or after the report. One expected-return field also contained
   `game-time decision` instead of a date. The original text is preserved, and
   same-day games are excluded from the before/after query.

## What I would flag for the vendor

- Use one stable player ID and team ID across every endpoint.
- Return complete box scores and clarify whether partial lineups are expected.
- Provide injury report timestamps with timezones and use consistent date fields.
- Use consistent non-2xx error responses and document retry and rate-limit rules.
