# Screens in Schools — school coverage dashboard

Tracks how many U.S. public schools have a Screens in Schools / Phones in Focus report, by state and level, and publishes a
static dashboard from `publish/`.

- `coverage_export.py` pulls only whitelisted, de-identified fields from Qualtrics (no email, IP, names or free text),
  matches responses to NCES school IDs, and writes `publish/` (page, aggregates, per-state school rosters).
- `build_schools.py` / `build_historical.py` build the school master and per-school counts from earlier waves.
- `data/` and `inputs/` hold public NCES lists and per-school counts only. Raw survey exports are never committed.
- The dashboard is `dashboard/index.html`; `.github/workflows/coverage-refresh.yml` refreshes `publish/` daily.

Internal documentation (definitions, decisions, setup notes) is kept separately by the team.
