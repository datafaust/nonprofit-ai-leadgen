# Plan

Phase 1: CLI → fixed NYC Open Data CSV adapters → bounded HTTPS download → pandas parsing and source-specific normalization → conservative identity matching → SQLite organization and source-record tables → safe CSV export. Dataset cells are untrusted; only the two configured Socrata URLs are fetched. Source-reported contacts are not verified.

Local viewer: a Flask/Jinja interface in `leads_web.py` lists organizations with pagination, name/description search, borough, reachable-website and current-site email filtering, and a detail page. It binds only to loopback. Dataset and draft text is rendered through autoescaped templates. The detail page has local draft creation and editing routes, protected by a form token.

Phase 2: manually invoked website reachability checks, followed by a bounded public general-inbox extractor for sites whose latest check is reachable. Each email is stored separately with its source page, extraction time, and unverified status. Scan history supports resume behavior. Further enrichment is deferred.

Phase 3: local Ollama analysis and evidence-backed RAG opportunity scoring. Model output requires schema validation.

Phase 4: local Ollama outreach drafts for individual human review, stored in SQLite. The detail page selects from locally installed generation models. No sending. Source facts are filtered and supplied as quoted data; model JSON is validated. Manual factual and compliance review remains required.

Phase 5: reporting, Excel output, and lead tracking.
