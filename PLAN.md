# Plan

Phase 1: CLI → fixed NYC Open Data CSV adapters → bounded HTTPS download → pandas parsing and source-specific normalization → conservative identity matching → SQLite organization and source-record tables → safe CSV export. Dataset cells are untrusted; only the two configured Socrata URLs are fetched. Source-reported contacts are not verified.

Local viewer: a read-only Flask/Jinja interface in `leads_web.py` lists organizations with pagination, name/description search, borough and reachable-website filtering, and a detail page. It binds only to loopback and has no write or outreach routes. Dataset text is rendered through autoescaped templates.

Phase 2, first increment: manually invoked website reachability checks, including an explicit `--all` run with per-site commits, progress output, resume behavior, and host pacing. Source-level website claims and check history remain separate from organization identity and contact verification. Further enrichment and public contact extraction are deferred.

Phase 3: local Ollama analysis and evidence-backed RAG opportunity scoring. Model output requires schema validation.

Phase 4: outreach drafts for individual human review. No sending.

Phase 5: reporting, Excel output, and lead tracking.
