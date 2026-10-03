import logging
import asyncio
import httpx
from typing import Optional, Dict, Any
from urllib.parse import urljoin

from .config import get_api_key, get_cache_config
from .cache import CacheManager

# Setup basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_URL = "https://api.elsevier.com/"

class ScopusClient:
    """
    Async client for interacting with the Elsevier Scopus API.
    Handles authentication, caching, rate limiting, and retries.
    """
    def __init__(self):
        self.api_key = get_api_key()
        self.cache_config = get_cache_config()
        self.headers = {
            'X-ELS-APIKey': self.api_key,
            'Accept': 'application/json',
            'User-Agent': 'ScopusMCP/0.1.0'
        }
        # Initialize CacheManager with default expiration
        self.cache = CacheManager(expiration_seconds=self.cache_config['default'])
        self.client = httpx.AsyncClient(
            headers=self.headers,
            timeout=30.0,
            follow_redirects=True
        )
        self.quota_info = {} # Store latest quota headers

    async def close(self):
        """Closes the underlying HTTP client."""
        await self.client.aclose()

    async def get_quota_status(self) -> Dict[str, Any]:
        """Returns the latest known quota status."""
        return self.quota_info

    async def _request(self, method: str, endpoint: str, params: Optional[Dict[str, Any]] = None, use_cache: bool = True, ttl: Optional[int] = None) -> Dict[str, Any]:
        """
        Internal method to handle API requests with caching, rate limiting, and retries.
        """
        url = urljoin(BASE_URL, endpoint)
        
        # Check cache (Synchronous cache access is fast enough)
        if use_cache and method.upper() == 'GET':
            cached = self.cache.get(url, params)
            if cached:
                logger.debug(f"Cache hit for {url}")
                return cached

        retries = 3
        backoff = 1

        while retries > 0:
            try:
                response = await self.client.request(method, url, params=params)
                
                # Update Quota Info from Headers
                self._update_quota_info(response.headers)
                
                # Handle Rate Limiting
                if response.status_code == 429:
                    reset_time = int(response.headers.get('X-RateLimit-Reset', asyncio.get_event_loop().time() + 60))
                    # Calculate wait time, ensure at least 1 second
                    # Note: asyncio time vs epoch time. X-RateLimit-Reset is usually epoch.
                    import time
                    current_time = time.time()
                    sleep_time = max(reset_time - current_time, 1)
                    
                    logger.warning(f"Rate limit exceeded. Sleeping for {sleep_time:.2f} seconds.")
                    await asyncio.sleep(sleep_time)
                    continue

                response.raise_for_status()
                data = response.json()

                # Save to cache if GET
                if use_cache and method.upper() == 'GET':
                    self.cache.set(url, data, params, ttl=ttl)

                return data

            except httpx.HTTPStatusError as e:
                # Update quota info even on error if headers exist
                if e.response:
                    self._update_quota_info(e.response.headers)
                    
                status = e.response.status_code
                if status in [500, 502, 503, 504]:
                    logger.warning(f"Server error {status}. Retrying in {backoff}s...")
                    await asyncio.sleep(backoff)
                    retries -= 1
                    backoff *= 2
                elif status == 401:
                     logger.error("Authentication failed. Check your API key.")
                     raise Exception("Authentication failed: Invalid API Key") from e
                elif status == 404:
                    logger.info(f"Resource not found: {url}")
                    return {} 
                else:
                    raise e
            except httpx.RequestError as e:
                logger.warning(f"Request failed: {e}. Retrying...")
                await asyncio.sleep(backoff)
                retries -= 1
                backoff *= 2
            except ValueError:
                logger.error("Failed to parse JSON response")
                raise Exception("Invalid JSON response from Scopus API")

        raise Exception(f"Max retries exceeded for {url}")

    def _update_quota_info(self, headers: httpx.Headers):
        """Updates internal quota state from response headers."""
        self.quota_info = {
            'limit': headers.get('X-RateLimit-Limit', 'unknown'),
            'remaining': headers.get('X-RateLimit-Remaining', 'unknown'),
            'reset': headers.get('X-RateLimit-Reset', 'unknown'),
            'status': 'OK'
        }

    async def search_scopus(self, query: str, count: int = 25, start: int = 0, sort: str = 'coverDate') -> Dict[str, Any]:
        """
        Searches Scopus API.
        Endpoint: content/search/scopus
        """
        params = {
            'query': query,
            'count': count,
            'start': start,
            'sort': sort,
            'view': 'STANDARD'
        }
        return await self._request('GET', 'content/search/scopus', params, ttl=self.cache_config['search'])

    async def get_abstract(self, scopus_id: str) -> Dict[str, Any]:
        """
        Retrieves abstract details.
        Endpoint: content/abstract/scopus_id/{id}
        """
        clean_id = scopus_id.replace('SCOPUS_ID:', '')
        return await self._request('GET', f'content/abstract/scopus_id/{clean_id}', ttl=self.cache_config['abstract'])

    async def get_author(self, author_id: str) -> Dict[str, Any]:
        """
        Retrieves author profile.
        Endpoint: content/author/author_id/{id}
        """
        clean_id = author_id.replace('AUTHOR_ID:', '')
        return await self._request('GET', f'content/author/author_id/{clean_id}', ttl=self.cache_config['author'])
