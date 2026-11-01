# Nonprofit AI Lead Generator

The project provides a local Python CLI for importing two public NYC datasets, normalizing records, storing provenance in SQLite, and exporting safe CSV. A small read-only web viewer displays the leads. A manually invoked checker can test listed website reachability. The application does not extract website content, use AI, score leads, or send messages.

## Setup and commands

Requires Python 3.12+ and `uv`. The recommended setup creates `.venv` and installs the locked project and test dependencies:

```sh
uv sync
uv run nonprofit-leadgen ingest
uv run nonprofit-leadgen stats
uv run nonprofit-leadgen export --borough Queens --output data/exports/queens_nonprofits.csv
uv run nonprofit-leadgen export --output data/exports/all_nonprofits.csv
uv run nonprofit-leadgen validate-websites --limit 5
uv run python leads_web.py
uv run pytest
```

To install from the pinned requirements files in a fresh environment instead:

```sh
uv venv --python 3.12
uv pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/python leads_web.py
```

Standard `venv` and `pip` also work (use a `python3` executable that is version 3.12 or newer):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

`requirements.txt` installs the application in editable mode plus its pinned runtime dependencies. `requirements-dev.txt` adds pinned test dependencies. For a runtime-only environment, install `requirements.txt` instead. Keep `pyproject.toml` and `uv.lock` as the dependency source of truth; after changing dependencies, regenerate the runtime file with `uv export --format requirements.txt --no-dev --no-hashes --locked --output-file requirements.txt` and update the development file from `uv export --format requirements.txt --only-dev --no-hashes --no-emit-project --locked`, retaining its `-r requirements.txt` line. Run installs from the project root because the runtime file contains `-e .`.

Open `http://127.0.0.1:5055/` for the local lead viewer. The list shows 50 organizations per page, with search and borough filtering; click an organization name for its details and source records. `uv run python leads_web.py --port 5056` uses another local port. Ingest data first. The viewer is read-only and makes no website requests; checking happens only through the CLI command. The email button on the detail page is disabled as a placeholder.

The lead list defaults to all organizations. Use **Website check → Reachable website only** to focus on organizations whose current listed URL had a latest check result of `reachable`. The filter combines with search and borough selection and remains active across pages. A reachable result is only a site response, not a verified organization or an outreach approval.

The viewer binds to `127.0.0.1` with debug mode off. It has no login because it is designed only for a trusted local machine; anyone with access to that machine can read the displayed data. Do not expose it through a public listener or port forward. HTML is escaped by Jinja, and the app sends restrictive browser security headers. Request access logs are suppressed to avoid logging search terms.

## Website reachability checks

`uv run nonprofit-leadgen validate-websites --limit 5` checks up to five currently listed organization websites and stores the results in SQLite. The command runs only when invoked; the maximum batch is 25. It skips URLs checked for the current claim within the last 30 days. Run `ingest` once after upgrading an older database to populate each source record's website claim. The lead list and detail page show the latest check for the currently listed URL.

To check **all currently eligible listed URLs in one manually started run**, use:

```sh
uv run nonprofit-leadgen validate-websites --all
```

This may take a long time across the full dataset. It prints and commits each result as it finishes, shows a status summary at the end, and respects per-host pacing and robots rules. Press Ctrl+C to stop; rerun the same command to resume. URLs with a check for the current claim in the last 30 days are skipped, including earlier errors. `--all` and `--limit` cannot be combined. The command does not run on a schedule or send outreach.

To inspect saved diagnostics for an ID printed by the checker, run `uv run nonprofit-leadgen website-checks --id 1234abcd` (replace the example prefix with yours). This reads recent checks without making new network requests. The CLI and detail page show bounded error categories such as `TimeoutError` or `DNS failed`; no raw response body or full page is logged.

Checks distinguish `reachable`, HTTP errors, network errors, robots restrictions, unsafe URLs, redirects to another domain, and non-HTML responses. A reachable site **does not verify organization identity, nonprofit status, or contact details**. A failed check does not remove the organization or its source-reported URL. Only check metadata (status, time, HTTP code, final URL, error type) is retained; page content is discarded. Check history remains local until the operator removes the database.

Website checking sends a GET for `robots.txt` and, if allowed, a bounded GET to the listed URL. The organization site sees the requester's IP address, requested path, and crawler user agent. The checker accepts only HTTP(S) domain names on standard ports, rejects private or reserved DNS answers, connects to the validated IP, verifies TLS hostnames, and checks redirects again. Redirects to other domains are not followed. It reads at most 1 KiB of a page and waits at least one second before requesting it. Review website terms before larger batches; some sites may block checks or have restrictive robots rules. No page links, scripts, or instructions are followed.

The database is automatically created at `data/nonprofits.db`. Database and exports stay local and are ignored by Git. No credentials or `.env` file are needed.

## Sources and data flow

- [NYC Community Based Organizations](https://data.cityofnewyork.us/Social-Services/NYC-Community-Based-Organizations/i4kb-6ab6): official Socrata CSV `i4kb-6ab6`; maps `organization_name`, `mission`, `street_address`, `city`, `state`, `postcode`, `website`, `borough`. No contact email or phone field.
- [NYC Women's Resource Network Database](https://catalog.data.gov/dataset/nyc-womens-resource-network-database): official Socrata CSV `pqg4-dm6b`; maps `organizationname`, `description`, `address1`, `city`, `postcode`, `url`, `phone`, `borough` and borough flags. Only rows marked `nonprofit=Y` are imported. The catalog says this dataset was last updated in 2018.

CSV downloads come only from fixed NYC Open Data API endpoints over HTTPS, requesting the built-in stable `:id` field. Downloads are bounded to 30 MiB with timeouts, a retry, a one-second delay between sources, and no redirects. Input columns are validated before storing. The `organizations` table holds normalized facts; `source_records` links every imported source row and its page URL, extraction method, and ingestion time. Matching across sources requires exact normalized name, street and ZIP. Missing fields remain null. Repeated imports of the same source ID update records. CSV exports prefix dangerous spreadsheet formula cells with an apostrophe.

Source-reported website and phone values are **unverified**. A listed URL is never fetched in Phase 1. No general organizational email is supplied by these fields. The women’s directory may be stale; verify contacts and organization status manually before outreach. Exact identity matching may leave real duplicates separate, by design. A website is only stored as a source claim, not proof of domain ownership.

The fixed API request has a 50,000-row limit; ingestion fails if that limit is reached, so pagination must be added before larger datasets are supported. The downloader rejects non-public DNS answers and redirects, but DNS resolution is not pinned to the connection, so DNS rebinding remains a limitation. Human review and a dependency vulnerability scan are recommended before sensitive deployment.

The CLI prints counts and error types, not raw records. Raw downloaded CSV is held in memory and not persisted. Any generated CSV may contain publicly listed contact details; handle it as local research data. Review the source terms and applicable outreach requirements before using the results.

The database and exports persist locally until the operator removes them. No automatic cleanup or remote backup is configured.
