# WDSF format notes (M0-equivalent discovery)

**Status: discovery only — no parser has been written against these yet.**

## Why this doc exists instead of an O2CM one

The spec's M0 called for O2CM discovery first. Before writing any parser we checked
`robots.txt` on all three O2CM hosts (a project non-negotiable — see spec section 2):
`results.o2cm.com`, the host that serves every actual per-event results page
(`event3.asp`, `individual.asp`), returns

```
User-agent: *
Disallow: /
```

That blocks automated access to the one O2CM host that matters. `o2cm.com` and
`events.o2cm.com` have no robots.txt (404) but only carry event listings, not results.
Per the user's decision, O2CM is deferred; WDSF is now the first source built end-to-end.
Revisit O2CM if/when permission or an alternate access route is sorted out.

WDSF itself had its own surprise: the documented REST API
(`services.worlddancesport.org/api/1/...`) returns `401` with `WWW-Authenticate: Basic`.
The API docs page (`worlddancesport.org/WDSF/IT-Infrastructure/WDSF-API`) confirms access
is granted case-by-case by WDSF's IT staff, not self-serve. So instead of the API, this
discovery targets the **public website** (`worlddancesport.org`, a different host from the
API subdomain). Its `robots.txt` is `User-agent: *` with no `Disallow`, and every page
below renders complete data server-side in plain HTML — no auth, no separate XHR/API calls
(confirmed via the browser network tab: loading `/Calendar/Competitions` fires zero requests
matching `api`, meaning the data ships embedded in the initial HTML response).

## Page types found, and their URL patterns

| Page | URL pattern | What it has |
|---|---|---|
| Competitions calendar | `/Calendar/Competitions` | Upcoming events, filterable by type/discipline/age group |
| Results calendar | `/Calendar/Results?Month=M&Year=Y` | Past events for a given month/year, links to event pages |
| Event page | `/Events/<City>-<Country>-<startDDMMYYYY>-[endDDMMYYYY-]<event-id>` | One competition stop; lists all comp_events (disciplines) held there, each linking to its Ranking/Marks/Officials pages |
| Ranking (results) | `/Competitions/Ranking/<slug>-<comp-event-id>` | Placements, round by round, with tie bands |
| Marks | `/Competitions/Marks/<slug>-<comp-event-id>` | **Individual judge marks**, per dance, per round — the highest-value data, present here |
| Officials | `/Competitions/Officials/<slug>-<comp-event-id>` | Maps judge letter codes (A, B, C…) used on the Marks page to adjudicator name + country |

The `<comp-event-id>` is a numeric ID stable across the Ranking/Marks/Officials pages for
one discipline — e.g. `Open-Taipei-Adult-Latin-66537` appears in all three URLs for that
one event's Latin competition. Treat `<slug>-<id>` as the natural key (`source_code`) for
`comp_event`, and the `<City>-<Country>-...-<event-id>` event slug as the natural key for
`competition`.

## Format stability across years

This is a single modern (Angular-rendered-server-side) site, not a decades-old system that
drifted — so unlike the spec's O2CM caveat ("formats drift across years"), **the same URL
patterns and roughly the same HTML structure serve results back to at least 2015**
(spot-checked 2015, 2019, 2024, 2026 — see `fixtures/wdsf/calendar_results_*.html`). This
significantly de-risks the WDSF parser: one parser should hold across the whole historical
range, modulo the field-size variant noted below.

## Ranking page: two structural variants, driven by field size

1. **Large field, multiple rounds** (`ranking_taipei_adult_latin_2026.html`, 30 couples):
   sections per round in reverse order (`Final`, `2. Round`, `1. Round`), each a table of
   `placement | couple names | country | start #`. Tie bands render as `"8. - 9."`. An
   `Excused couples` section lists no-shows/withdrawals separately (couple names + country,
   no placement).
2. **Small field, final round only** (`ranking_charlotte_seniorI_latin_2015.html`, 3 couples):
   no round sections at all — just one `Final` table, but with **different columns**:
   `placement | couple | country | start # | Base | Points` (a scoring-based ranking, not a
   skating-system placement list). Any parser must detect which column layout is present per
   table, not assume one shape.

## Marks page: confirms judge marks ARE available (spec open question #1, resolved)

The Marks page is a full skating-system matrix: one row group per couple (rowspan 2, one
row per round the couple danced), columns grouped by dance (Samba/Cha Cha/Rumba/Paso
Doble/Jive for Latin), sub-columns per judge letter (A through K+ depending on panel size),
cell values are either a numeric placement/mark or `+` (recalled/not marked in that
position), with a `=` majority-count column per dance and a running `Total`. This is exactly
the `mark` table's target shape: `(round, entry, judge, dance) -> placement/recalled`.

The Officials page must be joined in to resolve judge letters to `person` rows (name +
country given, no external ID visible on this page — WDSF MIN presumably requires the
gated API or an athlete profile page we haven't located a public route to yet).

Small finals-only events (Charlotte 2015) still have a Marks page, but it will need
checking whether it's the scoring-based Base/Points scheme instead of skating-system `+`
marks — not yet confirmed; flagged as a follow-up for the parser's test fixtures.

## Open items carried forward (mirrors spec section 10)

1. **Judge marks**: confirmed present (see above) — `mark` table is populated at parser
   time, not deferred.
2. **Format stability**: confirmed stable 2015–2026 on this site; the real variant axis is
   field size (rounds-based vs. final-only-with-points), not year.
3. **Machine-readable index**: no JSON index found; `/Calendar/Results?Month=&Year=` must be
   paged month by month to discover event slugs. `/Calendar/Legacy` (linked in nav) is
   unexplored — may be a different scheme for pre-2015 data, worth checking before assuming
   the modern URL pattern covers everything.
4. **Terms of use**: not yet reviewed (spec says answer before M4/anything public — noting
   here so it isn't forgotten, not blocking current single-user local use).
5. **Athlete profile pages / WDSF MIN**: `/Athletes` is a client-side search (submits via JS,
   no static links visible in raw HTML) — didn't find a public per-athlete profile URL in
   this pass. Entity resolution will start from names+country captured off Ranking/Marks
   pages; MIN-based auto-merge (spec's entity resolution step 1) is deferred until either the
   gated API or a profile-page route is found.

## Fixtures saved

All in `fixtures/wdsf/`, fetched with a descriptive User-Agent (contact email) and ≥1.5s
delay between requests, per spec politeness rules:

- `calendar_competitions_2026-08.html` — upcoming events calendar
- `calendar_results_2015-10.html`, `calendar_results_2019-10.html`, `calendar_results_2024-10.html` — historical month/year result listings (format-stability check)
- `event_taipei_2026.html` — event page, large multi-discipline stop
- `event_charlotte_2015.html` — event page, small historical stop
- `ranking_taipei_adult_latin_2026.html` — ranking, large field, multi-round
- `ranking_taipei_adult_standard_2026.html` — ranking, companion style (same event)
- `ranking_charlotte_seniorI_latin_2015.html` — ranking, small field, final-only, Base/Points variant
- `marks_taipei_adult_latin_2026.html` — full judge marks matrix, multi-round
- `marks_charlotte_seniorI_latin_2015.html` — judge marks, small final-only event
- `officials_taipei_adult_latin_2026.html` — judge letter → name/country mapping
