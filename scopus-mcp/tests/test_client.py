import unittest
from unittest.mock import MagicMock, patch, AsyncMock
from scopus_mcp.client import ScopusClient

class TestScopusClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Patch get_api_key to avoid needing config file in tests
        self.config_patcher = patch('scopus_mcp.client.get_api_key', return_value='fake_key')
        self.mock_get_key = self.config_patcher.start()
        
        # Patch CacheManager to avoid disk I/O
        self.cache_patcher = patch('scopus_mcp.client.CacheManager')
        self.MockCache = self.cache_patcher.start()
        self.mock_cache_instance = self.MockCache.return_value
        self.mock_cache_instance.get.return_value = None # Default no cache hit

        self.client = ScopusClient()

    async def asyncTearDown(self):
        self.config_patcher.stop()
        self.cache_patcher.stop()
        await self.client.close()

    @patch('scopus_mcp.client.httpx.AsyncClient.request')
    async def test_search_scopus_success(self, mock_request):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'search-results': {'entry': []}}
        
        # httpx.AsyncClient.request is awaitable
        mock_request.return_value = mock_response

        result = await self.client.search_scopus("AI")
        
        self.assertEqual(result, {'search-results': {'entry': []}})
        mock_request.assert_called_with('GET', 'https://api.elsevier.com/content/search/scopus', params={'query': 'AI', 'count': 25, 'start': 0, 'sort': 'coverDate', 'view': 'STANDARD'})

    @patch('scopus_mcp.client.httpx.AsyncClient.request')
    async def test_rate_limit_retry(self, mock_request):
        # First call 429, second 200
        response_429 = MagicMock()
        response_429.status_code = 429
        # Reset time in past so we don't sleep long
        response_429.headers = {'X-RateLimit-Reset': '0'} 

        response_200 = MagicMock()
        response_200.status_code = 200
        response_200.json.return_value = {'ok': True}

        mock_request.side_effect = [response_429, response_200]

        # Patch asyncio.sleep to avoid waiting
        with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            result = await self.client._request('GET', 'test')
            
            self.assertEqual(result, {'ok': True})
            self.assertEqual(mock_request.call_count, 2)
            mock_sleep.assert_called()
            
            self.assertTrue(mock_sleep.called)
            self.assertEqual(result, {'ok': True})
            self.assertEqual(mock_request.call_count, 2)

if __name__ == '__main__':
    unittest.main()
