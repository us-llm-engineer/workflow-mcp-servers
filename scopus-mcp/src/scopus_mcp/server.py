import asyncio
import contextlib
import json
import logging
import os
from datetime import date
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

from .client import ScopusClient
from .utils import clean_search_results, clean_search_results_full, clean_abstract_details, clean_author_profile

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scopus-mcp")

# Initialize Server
server = Server("scopus-mcp")
client = ScopusClient()

# --- search_recent_years: fixed constraint set + custom per-year pagination ---
#
# Constraint mapping (confirmed live against the real Scopus Search API):
#   Article title, Abstract, Keywords -> TITLE-ABS-KEY(...)
#   Subject area: Engineering, Computer Science -> SUBJAREA(ENGI OR COMP)
#   Document type: Article, Conference paper -> DOCTYPE(ar OR cp)
#   Language: English -> LANGUAGE(english)
#   Source type: Journal, Conference proceeding -> SRCTYPE(j OR p)
#   Open access: all -> OPENACCESS(1)
#   Range: exactly one year -> PUBYEAR = <year>, applied per year below
SEARCH_RECENT_YEARS_CONSTRAINTS = (
    "SUBJAREA(ENGI OR COMP) AND DOCTYPE(ar OR cp) AND LANGUAGE(english) "
    "AND SRCTYPE(j OR p) AND OPENACCESS(1)"
)
# Number of most recent years covered (current year down to current year - 3).
SEARCH_RECENT_YEARS_SPAN = 4
# Fixed number of results returned per year, per page. Not caller-settable
# -- see search_recent_years' tool description for why.
SEARCH_RECENT_YEARS_PAGE_SIZE = 5


def _build_recent_years_query(search_content: str, year: int) -> str:
    # search_content is matched as an exact phrase (quoted) -- confirmed
    # live that an unquoted multi-word TITLE-ABS-KEY query does a loose
    # keyword match that surfaces off-topic results (e.g. "agent system"
    # unquoted matched an architecture-practice paper via "agent" alone).
    phrase = search_content.replace('"', '')
    return f'TITLE-ABS-KEY("{phrase}") AND PUBYEAR = {year} AND {SEARCH_RECENT_YEARS_CONSTRAINTS}'

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_scopus",
            description="Search for documents in Scopus using a query string.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The Scopus search query (e.g., 'TITLE(AI) AND PUBYEAR > 2020')."
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 25).",
                        "default": 5,
                        "maximum": 25
                    },
                    "sort": {
                        "type": "string",
                        "description": "Sort order (e.g., 'coverDate', 'relevancy').",
                        "default": "coverDate"
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="get_abstract_details",
            description="Retrieve full details for a specific document by Scopus ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "scopus_id": {
                        "type": "string",
                        "description": "The Scopus ID of the document."
                    }
                },
                "required": ["scopus_id"]
            }
        ),
        types.Tool(
            name="get_author_profile",
            description="Retrieve an author's profile by Author ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "author_id": {
                        "type": "string",
                        "description": "The Scopus Author ID."
                    }
                },
                "required": ["author_id"]
            }
        ),
        types.Tool(
            name="get_citing_papers",
            description="Retrieve a list of papers that have cited the specified document (Forward Citations).",
            inputSchema={
                "type": "object",
                "properties": {
                    "scopus_id": {
                        "type": "string",
                        "description": "The Scopus ID of the document to find citations for."
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 25).",
                        "default": 5,
                        "maximum": 25
                    },
                    "sort": {
                        "type": "string",
                        "description": "Sort order (e.g., 'coverDate', 'relevancy').",
                        "default": "coverDate"
                    }
                },
                "required": ["scopus_id"]
            }
        ),
        types.Tool(
            name="get_quota_status",
            description="Get the current API quota status (remaining/limit). Note: Values are updated only after making a request.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        types.Tool(
            name="search_recent_years",
            description=(
                "Search Scopus for search_content across the last "
                f"{SEARCH_RECENT_YEARS_SPAN} years (current year down to "
                f"current year - {SEARCH_RECENT_YEARS_SPAN - 1}), one exact "
                "year at a time, with a fixed constraint set always applied: "
                "search_content is matched against Article title, Abstract, "
                "and Keywords; Subject area limited to Engineering and "
                "Computer Science; Document type limited to Article and "
                "Conference paper; Language limited to English; Source type "
                "limited to Journal and Conference proceeding; Open access "
                "limited to all open-access content. These constraints are "
                "NOT arguments -- they are always applied and cannot be "
                "changed per call; use search_scopus directly if you need "
                "different constraints. "
                "Pagination is PER YEAR, not across a single merged list: "
                f"page=1 returns each year's {SEARCH_RECENT_YEARS_PAGE_SIZE} "
                "most relevant results independently, page=2 returns each "
                "year's next batch, and so on -- so one call always returns "
                f"up to {SEARCH_RECENT_YEARS_SPAN} separate groups of "
                f"{SEARCH_RECENT_YEARS_PAGE_SIZE} results, one group per "
                "year, all at the same page number. Returns a JSON object "
                "(not Python repr, unlike this server's other tools) with "
                "results grouped and labeled by year -- see this tool's "
                "output shape by calling it once. Every result field "
                "actually obtainable at this API key's access tier is "
                "included (open-access label, document/source type, "
                "affiliations, ISSN/ISBN, a Scopus record link, and a "
                "doi.org link) -- EXCEPT abstract text, which Scopus does "
                "not return at STANDARD-tier API key access under any "
                "endpoint or view."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "search_content": {
                        "type": "string",
                        "description": (
                            "Search terms, matched as an exact phrase against "
                            "Article title, Abstract, and Keywords "
                            "(TITLE-ABS-KEY). E.g. 'agent system'."
                        )
                    },
                    "page": {
                        "type": "integer",
                        "description": (
                            f"Which page to fetch, independently within each "
                            f"of the {SEARCH_RECENT_YEARS_SPAN} years. page=1 "
                            f"is each year's first "
                            f"{SEARCH_RECENT_YEARS_PAGE_SIZE} results, page=2 "
                            f"the next {SEARCH_RECENT_YEARS_PAGE_SIZE}, and so "
                            "on. Must be >= 1."
                        ),
                        "default": 1,
                        "minimum": 1
                    }
                },
                "required": ["search_content"]
            }
        )
    ]

@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any] | None
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    if not arguments:
        arguments = {}

    try:
        if name == "search_scopus":
            query = arguments.get("query")
            count = arguments.get("count", 5)
            sort = arguments.get("sort", "coverDate")
            
            if not query:
                raise ValueError("Query is required")

            # Await the async client method
            raw_data = await client.search_scopus(query, count=count, sort=sort)
            results = clean_search_results(raw_data)
            
            return [types.TextContent(type="text", text=str(results))]

        elif name == "get_abstract_details":
            scopus_id = arguments.get("scopus_id")
            if not scopus_id:
                raise ValueError("scopus_id is required")
                
            raw_data = await client.get_abstract(scopus_id)
            details = clean_abstract_details(raw_data)
            
            return [types.TextContent(type="text", text=str(details))]

        elif name == "get_author_profile":
            author_id = arguments.get("author_id")
            if not author_id:
                raise ValueError("author_id is required")
                
            raw_data = await client.get_author(author_id)
            profile = clean_author_profile(raw_data)
            
            return [types.TextContent(type="text", text=str(profile))]

        elif name == "get_citing_papers":
            scopus_id = arguments.get("scopus_id")
            count = arguments.get("count", 5)
            sort = arguments.get("sort", "coverDate")

            if not scopus_id:
                raise ValueError("scopus_id is required")

            # Clean ID and construct REFEID query
            clean_id = scopus_id.replace('SCOPUS_ID:', '')
            query = f"REFEID({clean_id})"

            raw_data = await client.search_scopus(query, count=count, sort=sort)
            results = clean_search_results(raw_data)

            return [types.TextContent(type="text", text=str(results))]

        elif name == "search_recent_years":
            search_content = arguments.get("search_content")
            page = arguments.get("page", 1)

            if not search_content:
                raise ValueError("search_content is required")
            if not isinstance(page, int) or isinstance(page, bool) or page < 1:
                raise ValueError(f"page must be an integer >= 1 (got {page!r})")

            current_year = date.today().year
            years = [current_year - i for i in range(SEARCH_RECENT_YEARS_SPAN)]
            start = (page - 1) * SEARCH_RECENT_YEARS_PAGE_SIZE

            response = {
                "search_content": search_content,
                "page": page,
                "results_per_year_per_page": SEARCH_RECENT_YEARS_PAGE_SIZE,
                "years": {},
            }

            for year in years:
                query = _build_recent_years_query(search_content, year)
                try:
                    raw_data = await client.search_scopus(
                        query, count=SEARCH_RECENT_YEARS_PAGE_SIZE, start=start, sort="relevancy"
                    )
                    results = clean_search_results_full(raw_data)
                    total = int((raw_data or {}).get("search-results", {}).get("opensearch:totalResults", 0) or 0)
                    total_pages = -(-total // SEARCH_RECENT_YEARS_PAGE_SIZE) if total else 0
                    response["years"][str(year)] = {
                        "total_results": total,
                        "total_pages": total_pages,
                        "has_more_pages": page < total_pages,
                        "results": results,
                    }
                except Exception as year_error:
                    # A failure fetching one year must not lose the other
                    # years' results -- record it and keep going.
                    logger.error(f"search_recent_years: year {year} failed: {year_error}")
                    response["years"][str(year)] = {"error": str(year_error), "results": []}

            return [types.TextContent(type="text", text=json.dumps(response, indent=2, ensure_ascii=False))]

        elif name == "get_quota_status":
            quota = await client.get_quota_status()
            if not quota:
                return [types.TextContent(type="text", text="No quota information available yet. Please make a request to initialize.")]
            
            return [types.TextContent(type="text", text=str(quota))]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        logger.error(f"Error executing tool {name}: {e}")
        return [types.TextContent(type="text", text=f"Error: {str(e)}")]

@server.list_prompts()
async def handle_list_prompts() -> list[types.Prompt]:
    return [
        types.Prompt(
            name="research-summary",
            description="Search for papers on a topic and generate a research summary",
            arguments=[
                types.PromptArgument(
                    name="topic",
                    description="The research topic (e.g., 'machine learning healthcare')",
                    required=True
                )
            ]
        ),
        types.Prompt(
            name="author-analysis",
            description="Analyze an author's research impact and recent work",
            arguments=[
                types.PromptArgument(
                    name="author_id",
                    description="The Scopus Author ID",
                    required=True
                )
            ]
        )
    ]

@server.get_prompt()
async def handle_get_prompt(
    name: str, arguments: dict[str, str] | None
) -> types.GetPromptResult:
    if not arguments:
        arguments = {}

    if name == "research-summary":
        topic = arguments.get("topic", "unknown topic")
        return types.GetPromptResult(
            description=f"Research summary for {topic}",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(
                        type="text",
                        text=f"Please search specifically for high-cited papers related to '{topic}' published in the last 5 years using the search_scopus tool. Sort by cited references if possible. After retrieving the results, please summarize the key trends and findings in this field."
                    )
                )
            ]
        )

    if name == "author-analysis":
        author_id = arguments.get("author_id", "")
        return types.GetPromptResult(
            description=f"Analysis of author {author_id}",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(
                        type="text",
                        text=f"Please call the get_author_profile tool for Author ID '{author_id}'. Based on the returned data, analyze their research impact (citations, h-index if available), identify their main affiliation, and summarize their academic standing."
                    )
                )
            ]
        )

    raise ValueError(f"Unknown prompt: {name}")

async def main():
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options()
            )
    finally:
        # Ensure client is closed on shutdown
        await client.close()

class _BearerAuthMiddleware:
    """Raw ASGI middleware: rejects any HTTP request that doesn't carry
    `Authorization: Bearer <secret>`. Only used in streamable-http mode --
    stdio has no network exposure to protect."""

    def __init__(self, app, secret: str):
        self.app = app
        self.secret = secret

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode("latin-1")
        if auth != f"Bearer {self.secret}":
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        await self.app(scope, receive, send)

async def run_streamable_http(host: str, port: int, secret: str) -> None:
    """Serve this same tool set over the network via Streamable HTTP,
    instead of spawning as a local stdio subprocess. Needed for a caller
    whose agent runs in a separate sandbox/VM (e.g. a cloud sandbox) -- stdio only works when server and caller share a
    machine.

    Uses the low-level Server API's own StreamableHTTPSessionManager
    (this server predates FastMCP's one-line transport="streamable-http"
    convenience), exposed
    at exactly POST/GET/DELETE /mcp (no trailing slash) and served with
    uvicorn.

    Registered via Route, not Mount: a plain function passed to Route is
    treated as a Request/Response-style handler, not raw ASGI (confirmed
    by reading Starlette's own Route.__init__) -- wrapping it in a small
    callable class instead is what makes Starlette treat it as ASGI. Mount
    was tried first and rejected: it 307-redirects a bare POST /mcp to
    /mcp/ (confirmed live), which MCP clients should not have to know to
    work around.
    """
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Route
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    session_manager = StreamableHTTPSessionManager(app=server, stateless=True)

    class MCPEndpoint:
        async def __call__(self, scope, receive, send):
            await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with session_manager.run():
            yield

    starlette_app = Starlette(
        routes=[Route("/mcp", endpoint=MCPEndpoint(), methods=["GET", "POST", "DELETE"])],
        lifespan=lifespan,
    )
    secured_app = _BearerAuthMiddleware(starlette_app, secret)
    config = uvicorn.Config(secured_app, host=host, port=port, log_level="info")

    try:
        await uvicorn.Server(config).serve()
    finally:
        await client.close()

def start():
    """Entry point for the package script. Defaults to stdio (for local
    MCP clients that spawn this as a subprocess on the same machine --
    stdio requires that). Set MCP_TRANSPORT=streamable-http to instead
    serve over the network on MCP_HTTP_HOST:MCP_HTTP_PORT (default
    127.0.0.1:8766).

    In streamable-http mode, MCP_SHARED_SECRET is required: every request
    must carry `Authorization: Bearer <that secret>`, checked before any
    tool runs. This server holds a real Scopus API key -- an
    unauthenticated network-reachable copy of it lets anyone burn your
    quota, not a hardening nicety.
    """
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        asyncio.run(main())
    elif transport == "streamable-http":
        secret = os.environ.get("MCP_SHARED_SECRET")
        if not secret:
            raise ValueError(
                "MCP_SHARED_SECRET is required for streamable-http mode -- this server "
                "holds a real Scopus API key, and streamable-http means network-reachable. "
                "Set it to a long random value and configure the same value as an "
                "Authorization: Bearer header on the connecting client."
            )
        host = os.environ.get("MCP_HTTP_HOST", "127.0.0.1")
        port = int(os.environ.get("MCP_HTTP_PORT", "8766"))
        asyncio.run(run_streamable_http(host, port, secret))
    else:
        raise ValueError(f"Unknown MCP_TRANSPORT {transport!r}; use 'stdio' or 'streamable-http'")

if __name__ == "__main__":
    start()
