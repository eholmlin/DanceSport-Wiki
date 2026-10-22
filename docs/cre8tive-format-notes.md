# Cre8tive Score format notes (discovery)

**Status: loader built** — `src/dsr/parse/cre8tive.py`, `src/dsr/load/cre8tive.py`, `scripts/bulk_load_cre8tive.py` (`--list`, `--only <substring>`, `--dry-run`). Monday (Oct 5) loaded into the local DB as a test: 21 events, 61 rounds, 762 entries, 81,923 marks.

Cre8tive Score is the scoring platform that ran the International Dance Festival 2026
(UK, Oct 3–8 2026) and the Platinum Dance Awards (Sep 2026). Its public
results site is `https://cre8tive-result.vercel.app` (a Next.js single-page app). Unlike
NDCA/WDSF, results are not in the HTML: the SPA fetches JSON from a separate host.

## Access / etiquette

- `robots.txt`: `cre8tive-result.vercel.app` → 404, `cre8tive-score.vercel.app` → 404
  (the latter returns the app's HTML shell). Neither disallows anything.
- The JSON endpoints are the ones the public site itself calls, return
  `access-control-allow-origin: *`, and need no auth or cookies. They are undocumented,
  so expect them to change without notice.
- Fetch politely (low rate, identifying User-Agent, cache raw responses) as with the other sources.
- Not yet checked: any terms-of-use statement on the site. The organizers were emailed
  about data use; a follow-up should credit Cre8tive Score and the festival.

## Endpoints

Base: `https://cre8tive-score.vercel.app`

| Endpoint | Returns |
|---|---|
| `GET /api/public-calendar` | `{competitions: [...]}` — every published competition (currently 8: the 7 festival days/sessions + Platinum Dance Awards). Each has `id`, `name`, `date` (`YYYY/M/D`), `eventCount`, `competitorCount`, `resultsPublished`, `hideJudgeScores`, `status`, `judgeNames`. |
| `GET /api/public-snapshots/<competition-id>?kind=<kind>[&eventId=..][&sku=..]` | `{source:"cre8tive", competitionId, kind, data}`. Only `kind`, `eventId`, `sku` query params are accepted. |

Snapshot `kind` values (from the app's JS): `meta`, `schedule`, `result-index`,
`result-headers`, `result-headers-eligibility`, `competitors`, `result-summary`,
`final-summary`, `result-detail`.

| kind | Needs | Contents |
|---|---|---|
| `meta` | – | Competition id, name, date, judge id→name map (`judgeNames`), `hideJudgeScores`, publish flags |
| `schedule` | – | Timetable of events (number, category, time, zone, rounds, dances, competitors, judges) |
| `result-index` | – | List of every `eventId` with results (one per round: `...-auto-2r`, `-qf`, `-sf`, `-f`) — the loop driver |
| `result-headers-eligibility` | – | All rounds' recall summaries in one response: per couple `numberLabel`, `picksPerDance`, `totalMarks`, `advanced` (~200 KB for a 762-couple day) |
| `competitors` | optional `sku` | Every couple: `bibNumber`, `name`, `categoryName`, `sku`, `representingUnit` (usually empty) |
| `final-summary` | `eventId` | Event info + `overallFinalResults` (placement, per-dance ranks, rank sum, tie-break note) |
| `result-detail` | `eventId` | Everything in `final-summary` plus `perDanceResults`: per dance, the judge list and each couple's **per-judge placements** (`judgeMarks`, same order as `judges`), `placementCounts`, `placementSums`, `majorityRank`, `finalRankInDance`, tie-break info; plus `eventScores` |
| `result-summary` | `eventId` | For non-final rounds: event info (and `eventScores.roundResultsSummary` for recalls) |

## Shape notes

- Competition ids are per **day/session**, not per festival (e.g.
  `2026105-international-dance-festival-2026-monday-dc47a543`). A loader has to decide
  whether to group the seven festival ids into one `Competition` or load one per day.
- Event ids look like `<timestamp>-event-<n>-<sku>-auto-<round>`; `sku` (e.g. `M005`) ties
  an event to its competitors, `eventNumber` is the printed event number.
- Competitors are listed as one string, `"NIKITA VASILEV AND DIANA NAGAITSEVA"` — upper
  case, no country/club (`representingUnit` empty), so person-resolution has less to
  go on than NDCA/WDSF. Judges have real names (`id` → `name`) and are consistent
  within a competition's `judgeNames`.
- Final per-judge marks are Skating System placements (1..N), exactly what the existing
  `Mark.placement` model holds.
- Recall rounds (`-2r`/`-qf`/`-sf`) have no `result` block, but `result-detail`'s
  `eventScores` carries per-judge data keyed `judge_<id>`: `picks` = `{danceIndex: [couple
  ids recalled]}` (the Y marks) and `hesitations` = couples that were close calls, plus
  `roundResultsSummary` (totals, `advanced`). So per-judge recall marks are available too.
  Couple ids in picks are the numeric bib/`coupleId`, matching `competitors.bibNumber`.
- `hideJudgeScores: true` (Platinum Dance Awards) means individual judge marks are
  withheld there; the festival days have `false`.
- Observed Monday: 101 events, 762 couples; final `result-detail` ≈ 19 KB each. A full
  festival day is a few hundred small requests plus one `result-headers-eligibility`.

## Open questions

- Do older editions exist? The calendar only lists 2026 so far.
- Terms of use / explicit permission (follow up with organizers).

## Loader decisions

- One `Competition` per API competition (per day/session); `source_code` = the API id.
- One `CompEvent` per `sku`, titled with the category (`" [sku]"` appended if two skus share a category in one competition). Rounds come from each event's `rounds` field (1R/2R/3R/QF/SF/F), not from the event-id suffix, since ids vary (`-auto-sf`, `event-timetable-…`, `carry-event-…`).
- Recall rounds: every judge listed for the event who submitted gets a Y/N mark per couple per dance (`recalled`); the round's per-couple Y count equals the source's own `totalMarks` (tested). Each judge's `hesitations` (couples flagged per dance) are loaded into the separate `mark_hesitation` table (migration `c3e1f5a7b9d2`; must be applied to production with `alembic upgrade head` before anything reads it there). They don't count toward the totals and only some flagged couples are also picks; the meaning is undocumented, so nothing in the app interprets them yet.
- Finals: per-judge placements, dance names mapped to the same "Int'l Waltz" / "Amer. Rumba" forms the other sources use.
- Couples that appear in a round's summary but not in the competitor list (e.g. Monday M006, couple 38) are skipped and counted.
- Non-finalists get a result with no placement (same as NDCA); finalists get their overall final rank.
- Names are title-cased from upper case; `** ` prefixes are stripped; no `AND` means a solo entry. Judge titles (Mr/Ms) are stripped.
- Not wired into production sync: `scripts/sync_results_to_production.py` only handles `ndca_premier`.
