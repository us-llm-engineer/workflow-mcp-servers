# scopus-mcp: implementation

This is a local copy of the open-source [`qwe4559999/scopus-mcp`](https://github.com/qwe4559999/scopus-mcp) (MIT; its `LICENSE`, `README.md`, `README_CN.md` metadata and `server.json` are kept) with these local additions: the `search_recent_years` tool, structured output for it (`clean_search_results_full`), the optional authenticated streamable-HTTP transport, and the tests for them. The package is `scopus_mcp` (Python >= 3.10, `httpx`, `mcp`, `python-dotenv`), about 1,000 lines in `src/scopus_mcp/`.

## Modules

| File | Role |
| --- | --- |
| `server.py` | Low-level MCP `Server`: tool and prompt definitions, dispatch, stdio and streamable-HTTP transports. |
| `client.py` | `ScopusClient`: async Elsevier API client with caching, quota tracking, rate-limit handling and retries. |
| `cache.py` | `CacheManager`: on-disk JSON cache keyed by request hash. |
| `config.py` | API key and TTL resolution. |
| `utils.py` | Reduces verbose Scopus JSON to compact records (search, abstract, author). |

## Configuration

- **API key:** `SCOPUS_API_KEY` environment variable first, then `api_key` in `config.json` (copy `config.json.example`; `config.json` is git-ignored). Missing key raises a clear error at client construction.
- **Cache TTLs** (env `CACHE_TTL_*` or `config.json`): search 3,600 s, abstract 30 days, author 7 days, default 24 h.
- **Transport:** `MCP_TRANSPORT=stdio` (default) or `streamable-http` (needs `MCP_SHARED_SECRET`; `MCP_HTTP_HOST` default `127.0.0.1`, `MCP_HTTP_PORT` default 8766).

## Client behaviour (`client.py`)

All three endpoints go through one `_request` method:

1. **Cache first** for GET requests. `CacheManager` hashes URL + JSON-serialised sorted params with SHA-256, stores `{timestamp, ttl, data}` as a file under `~/.cache/scopus-mcp/`, and treats an entry as a miss when it is expired, unreadable, or corrupt. Write failures are swallowed so caching can never break a query. Each endpoint passes its own TTL.
2. **Quota tracking.** After every response (including errors) the `X-RateLimit-Limit/Remaining/Reset` headers are stored and served by `get_quota_status`.
3. **429 handling.** Sleeps until `X-RateLimit-Reset` (epoch seconds, at least one second) and retries.
4. **Retries.** Up to three attempts with exponential backoff (1, 2, 4 s) for 500/502/503/504 and transport errors; 401 raises "Authentication failed: Invalid API Key"; 404 returns an empty result; other statuses raise.

Endpoints: `content/search/scopus` (STANDARD view), `content/abstract/scopus_id/{id}` and `content/author/author_id/{id}`. Identifiers are accepted with or without their `SCOPUS_ID:` / `AUTHOR_ID:` prefix.

## Tools

| Tool | Behaviour |
| --- | --- |
| `search_scopus(query, count, sort)` | Raw Scopus query syntax; compacted by `clean_search_results` (id, title, creator, publication, cover date, DOI, citation count, aggregation type, Scopus link). |
| `get_abstract_details(scopus_id)` | Abstract-retrieval record, compacted. |
| `get_author_profile(author_id)` | Author profile with affiliation. |
| `get_citing_papers(scopus_id, count, sort)` | Implemented as the search `REFEID(<id>)`, i.e. documents that reference the given record. |
| `get_quota_status()` | Latest rate-limit headers, or a message if no request was made yet. |
| `search_recent_years(search_content, page)` | See below. |

Two prompts, `research-summary` and `author-analysis`, are also registered.

### `search_recent_years`

A fixed-shape literature scan, deliberately without free constraint arguments. For each of the **last four calendar years** (current year down to current year - 3) it runs

```
TITLE-ABS-KEY("<phrase>") AND PUBYEAR = <year>
  AND SUBJAREA(ENGI OR COMP) AND DOCTYPE(ar OR cp) AND LANGUAGE(english)
  AND SRCTYPE(j OR p) AND OPENACCESS(1)
```

sorted by relevancy, five results per year per page. The phrase is **quoted** because an unquoted multi-word `TITLE-ABS-KEY` query was observed to do a loose keyword match that surfaces off-topic papers. Pagination is *per year*: page 2 returns each year's next five, so one call returns up to four labelled groups. Each group reports `total_results`, `total_pages` and `has_more_pages`; a failure fetching one year is recorded as that year's `error` and does not discard the other years. The output is JSON (the other tools return Python `repr`), built by `clean_search_results_full`, which adds open-access label, document and source type, affiliations, ISSN/eISSN/ISBN, volume/issue/pages, a Scopus link and a `doi.org` link. It deliberately omits abstract text: abstracts are not available at standard-tier API key access under any endpoint or view, a limitation confirmed against the live API.

## Streamable HTTP mode

For agents that run in a separate VM and cannot spawn a local stdio process, `run_streamable_http` serves the same tool set at `POST/GET/DELETE /mcp` using the SDK's `StreamableHTTPSessionManager` (stateless) under Starlette and uvicorn. The route is registered with `Route` plus a callable ASGI class rather than `Mount`, because `Mount` redirects a bare `POST /mcp` to `/mcp/` with a 307, which MCP clients should not have to handle. A raw ASGI middleware rejects any HTTP request whose `Authorization` header is not exactly `Bearer <MCP_SHARED_SECRET>` with 401 before any tool runs; the secret is mandatory because the process holds a real API key and an open endpoint would let anyone burn the quota.

## Tests

`pytest` runs 23 tests: the client (a successful search and 429 rate-limit retry), the result parsers (including every field of the full record and missing-field tolerance), and `search_recent_years` (exact-phrase query construction, fixed constraints, page validation and offsets, one group per year, `has_more_pages`, per-year error isolation, relevancy sort) against mocked client calls; no network or API key is needed.
