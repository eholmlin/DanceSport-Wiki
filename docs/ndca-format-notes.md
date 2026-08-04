# NDCA Premier format notes (discovery)

**Status: discovery only, then a full parser was built against these fixtures.**

## Access

- `robots.txt`: 404 (none) on `ndcapremier.com`.
- No explicit terms-of-use page found on the results site itself (just a
  disclaimer that people-search only covers competitors from the selected
  season, and a link out to NDCA.org's general privacy policy). No detected
  restriction on automated access, unlike WDC (see below).
- Contrast: `wdcdance.com` (World Dance Council, whose Amateur League is
  "WDCAL") sits behind an active Cloudflare bot-challenge (403 + JS
  challenge to plain requests) — a real technical control, not just an
  absent robots.txt. Deliberately not pursued as a source; flagged to the
  user rather than worked around.

## It's a JSON API, not scraped HTML

Unlike O2CM/WDSF, results are served from a clean JSON feed
(`ndcapremier.com/feed/...`), not HTML tables. This is a significant
simplification: no selector fragility, no redirect gotchas (cf. WDSF's
query-string-dropping www redirect), and judge marks + officials are already
embedded in the same response — no separate pages to fetch and correlate.

## Endpoints used

| Endpoint | Purpose |
|---|---|
| `/feed/compyears/?season=<N>` | List competitions for a season (`Comp_Year_ID` = our competition natural key) |
| `/feed/compyears/?cyi=<id>` | Metadata for one competition instance (name, dates, location, publish flags) |
| `/feed/results/?cyi=<id>` | Roster: every competitor `{ID, Name}` who competed at this competition |
| `/feed/results/?cyi=<id>&id=<competitor_id>` | Full results for one competitor: every event they entered, with rounds, dances, judges, marks, and **all other competitors in the same heat** (confirmed by comparing two different competitors' fetches for the same event — the "Competitors" array under a dance is the whole heat, not just the queried person) |
| `/feed/results/?cyi=<id>&event=<event_id>` | Same round/dance/judge/mark data, scoped to one event directly — used once an event ID is known, avoids re-fetching it via every competitor who danced it |

**No full event-index endpoint was found** (checked `/feed/program/` and
`/feed/heatlists/` — both only return attendee/session metadata, not an event
list). Enumerating every event at a competition therefore requires fetching
every competitor's individual results and taking the union of Event IDs seen
— see `docs/ndca-format-notes.md` follow-up on bulk-load cost below.

## Data shape

```
Competitor: {ID: "A203", Name: [given, family], Keywords: <studio/affiliation free text>,
             Gender, Pro_Am_Status: "A"|"P", Server_ID}
Event: {ID, Name, Heat, Type: "Couple"|"Single", Classification: "Multi"|"Single",
        Floor, Entries, Is_Circuit, Rounds: [...]}
Round: {ID, Name (e.g. "Semi-Final", "Final"), Date_Time, Session_ID,
        Scoring_Method: "Prelims"|"Skated", Dances: [...]}
Dance: {Dance_ID, Dance_Name, Dance_Abbreviation, Judges: [{ID, Judge_Letter, Name}],
        Competitors: [...]}
Competitor-in-dance: {ID: "C151" (couple/entry id, different namespace from athlete "A" ids),
                      Bib, Marks: [...], Result: <placement, only set on Skated rounds>,
                      Participants: [{ID: "A..", Name}, ...]}
```

Two scoring methods confirmed:
- **"Prelims"** — binary marks per judge (`1`=recalled, `0`=not), `Result` is
  null. Maps directly onto `mark.recalled`, same idea as WDSF's `+`/blank.
- **"Skated"** — ordinal placement per judge, `Result` is the round's overall
  placement for that competitor. Maps onto `mark.placement` and, for the
  chronologically-last round, `result.placement_low/high`.

Rounds appear in the JSON already in chronological order (earliest round
first), unlike WDSF's ranking page which lists Final first. `round_order` is
just the array index + 1.

## Identity: NDCA's ids are NOT reliable external ids (important difference from WDSF)

WDSF's per-athlete GUID is embedded in a canonical profile URL and is
obviously a real, permanent identifier. NDCA's competitor id (`"A203"`) and
judge id (`14`) are small sequential integers with no such guarantee. Tested
whether the same real person keeps the same id across different competitions
(fetched three different years of the same recurring competition, "Austin
Star Ball" 2024/2025/2026, looking for a specific competitor) — inconclusive
because that specific person didn't appear in all three years, and a second
cross-competition check (matching competitor and judge names across two
unrelated competitions) found zero name overlap to compare. No positive
evidence of stable cross-competition ids was found, and the pattern (small
sequential integers, reset-looking ranges) is consistent with per-competition
registration ids rather than a persistent membership id — the spec's actual
"NDCA number" is a separate, more official identifier this feed doesn't
appear to expose.

**Decision**: NDCA competitor/judge ids are treated as competition-scoped,
*not* stored as `person.wdsf_min`-equivalent external refs. Entity resolution
for this source relies on name-based matching (exact alias, then the fuzzy
resolver) as primary — exactly the case the fuzzy resolver was built for.
This is a judgment call under uncertainty, documented rather than asserted as
fact; revisit if a genuine NDCA-number field is found elsewhere on the site.

## Bulk-load cost note

Since there's no event index, loading one competition completely means
fetching every one of its competitors individually (confirmed each fetch
returns the *whole heat*, so this over-fetches redundantly but does
guarantee full coverage). A competition with ~130 entrants means ~130
requests at the project's 1.5s/request politeness delay — around 3-4 minutes
per competition. Fine for one test competition; a full multi-competition
bulk load will take proportionally longer than WDSF's per-comp_event
approach (~4 requests/comp_event there vs ~1 request/competitor here).
