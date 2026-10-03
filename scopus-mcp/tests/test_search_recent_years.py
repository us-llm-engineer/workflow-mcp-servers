import json
import os
import re
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

# scopus_mcp.server builds a module-global ScopusClient() at import time,
# which calls get_api_key() immediately -- set a dummy key via env var
# BEFORE importing so this test file never depends on a real config.json
# or a real key being present (env var takes precedence in get_api_key()).
os.environ.setdefault('SCOPUS_API_KEY', 'test-key-not-real')

from scopus_mcp.server import (
    handle_call_tool,
    _build_recent_years_query,
    SEARCH_RECENT_YEARS_SPAN,
    SEARCH_RECENT_YEARS_PAGE_SIZE,
)


def expected_years():
    current = date.today().year
    return [current - i for i in range(SEARCH_RECENT_YEARS_SPAN)]


def canned_response(year, total_results):
    """A minimal, realistic-shaped response for one year, with one
    result whose title embeds the year so tests can verify each year's
    group actually came from that year's own query."""
    return {
        'search-results': {
            'opensearch:totalResults': str(total_results),
            'entry': [
                {
                    'dc:identifier': f'SCOPUS_ID:{year}0001',
                    'dc:title': f'Paper from {year}',
                    'prism:coverDate': f'{year}-01-01',
                    'link': [{'@ref': 'scopus', '@href': f'https://scopus.example/{year}'}],
                }
            ],
        }
    }


def recording_fake_search(calls, total_results=1):
    """Builds an async fake for client.search_scopus that records every
    call's arguments into `calls` and returns a canned per-year response.

    Deliberately does NOT assert inside the coroutine itself: the tool's
    own per-year try/except (added for fault tolerance -- see
    test_one_years_api_failure_does_not_lose_the_other_years) will catch
    an AssertionError raised in here just like any other exception and
    silently record it as that year's "error" instead of failing the
    test. Assertions belong in the test body, after awaiting
    handle_call_tool, against the recorded `calls` list.
    """
    async def fake_search(query, count, start, sort):
        year = int(re.search(r'PUBYEAR = (\d+)', query).group(1))
        calls.append({'year': year, 'query': query, 'count': count, 'start': start, 'sort': sort})
        return canned_response(year, total_results=total_results)

    return fake_search


class TestBuildRecentYearsQuery(unittest.TestCase):
    def test_wraps_search_content_as_exact_phrase(self):
        q = _build_recent_years_query('agent system', 2024)
        self.assertIn('TITLE-ABS-KEY("agent system")', q)

    def test_strips_embedded_quotes_to_avoid_breaking_the_query(self):
        q = _build_recent_years_query('agent "system"', 2024)
        self.assertIn('TITLE-ABS-KEY("agent system")', q)
        self.assertNotIn('""', q)

    def test_includes_exact_year_and_all_fixed_constraints(self):
        q = _build_recent_years_query('x', 2021)
        self.assertIn('PUBYEAR = 2021', q)
        for clause in [
            'SUBJAREA(ENGI OR COMP)',
            'DOCTYPE(ar OR cp)',
            'LANGUAGE(english)',
            'SRCTYPE(j OR p)',
            'OPENACCESS(1)',
        ]:
            self.assertIn(clause, q)


class TestSearchRecentYearsTool(unittest.IsolatedAsyncioTestCase):
    def assert_no_year_errors(self, data):
        """Guards every happy-path test against the exact masking bug
        found while writing this suite: a raised AssertionError inside a
        mock's side_effect gets swallowed by the tool's own per-year
        try/except and recorded as a silent per-year "error" instead of
        failing the test. Call this whenever a test's mock is expected to
        succeed for every year, so a masked failure still fails loudly."""
        for year, group in data['years'].items():
            self.assertNotIn('error', group, f"year {year} silently errored: {group.get('error')}")

    async def test_no_search_content_rejected_without_calling_the_api(self):
        with patch('scopus_mcp.server.client.search_scopus', new_callable=AsyncMock) as mock_search:
            result = await handle_call_tool('search_recent_years', {})
        self.assertIn('Error', result[0].text)
        self.assertIn('search_content is required', result[0].text)
        mock_search.assert_not_called()

    async def test_non_positive_page_rejected_without_calling_the_api(self):
        for bad_page in (0, -1, -100):
            with patch('scopus_mcp.server.client.search_scopus', new_callable=AsyncMock) as mock_search:
                result = await handle_call_tool(
                    'search_recent_years', {'search_content': 'agent system', 'page': bad_page}
                )
            self.assertIn('Error', result[0].text)
            mock_search.assert_not_called()

    async def test_default_page_is_1_and_uses_offset_0(self):
        calls = []
        with patch('scopus_mcp.server.client.search_scopus', side_effect=recording_fake_search(calls)):
            result = await handle_call_tool('search_recent_years', {'search_content': 'agent system'})

        data = json.loads(result[0].text)
        self.assert_no_year_errors(data)
        self.assertEqual(data['page'], 1)
        self.assertEqual(len(calls), SEARCH_RECENT_YEARS_SPAN)
        for call in calls:
            self.assertEqual(call['start'], 0)

    async def test_page_2_uses_the_correct_offset_and_count(self):
        calls = []
        with patch('scopus_mcp.server.client.search_scopus', side_effect=recording_fake_search(calls, total_results=100)):
            result = await handle_call_tool(
                'search_recent_years', {'search_content': 'agent system', 'page': 2}
            )

        data = json.loads(result[0].text)
        self.assert_no_year_errors(data)
        self.assertEqual(data['page'], 2)
        self.assertEqual(len(calls), SEARCH_RECENT_YEARS_SPAN)
        for call in calls:
            self.assertEqual(call['start'], SEARCH_RECENT_YEARS_PAGE_SIZE)  # page 2 -> offset = page_size
            self.assertEqual(call['count'], SEARCH_RECENT_YEARS_PAGE_SIZE)

    async def test_response_is_valid_json_with_one_group_per_year(self):
        """Unlike this server's other tools (which return Python repr via
        str()), this tool must return real, parseable JSON."""
        calls = []
        with patch('scopus_mcp.server.client.search_scopus', side_effect=recording_fake_search(calls)):
            result = await handle_call_tool('search_recent_years', {'search_content': 'agent system'})

        data = json.loads(result[0].text)  # raises if not valid JSON
        self.assert_no_year_errors(data)
        self.assertEqual(data['search_content'], 'agent system')
        self.assertEqual(data['results_per_year_per_page'], SEARCH_RECENT_YEARS_PAGE_SIZE)
        self.assertEqual(sorted(data['years'].keys()), sorted(str(y) for y in expected_years()))
        self.assertEqual(len(data['years']), SEARCH_RECENT_YEARS_SPAN)

    async def test_each_year_group_actually_comes_from_that_years_own_query(self):
        """Not just 'N groups exist' -- each year's group must reflect
        results from a query that actually asked for that exact year,
        not e.g. all 4 groups accidentally sharing one response."""
        calls = []
        with patch('scopus_mcp.server.client.search_scopus', side_effect=recording_fake_search(calls)):
            result = await handle_call_tool('search_recent_years', {'search_content': 'agent system'})

        data = json.loads(result[0].text)
        self.assert_no_year_errors(data)
        for year in expected_years():
            group = data['years'][str(year)]
            self.assertEqual(len(group['results']), 1)
            self.assertEqual(group['results'][0]['title'], f'Paper from {year}')
        # And the calls each carried that exact year's PUBYEAR clause.
        called_years = sorted(c['year'] for c in calls)
        self.assertEqual(called_years, sorted(expected_years()))

    async def test_exactly_span_calls_made_to_the_api(self):
        calls = []
        with patch('scopus_mcp.server.client.search_scopus', side_effect=recording_fake_search(calls)) as mock_search:
            await handle_call_tool('search_recent_years', {'search_content': 'agent system'})

        self.assertEqual(mock_search.call_count, SEARCH_RECENT_YEARS_SPAN)

    async def test_has_more_pages_true_when_more_remain(self):
        calls = []
        # 100 total results, page size 5 -> definitely more than one page
        with patch('scopus_mcp.server.client.search_scopus', side_effect=recording_fake_search(calls, total_results=100)):
            result = await handle_call_tool(
                'search_recent_years', {'search_content': 'agent system', 'page': 1}
            )

        data = json.loads(result[0].text)
        self.assert_no_year_errors(data)
        for year in expected_years():
            self.assertTrue(data['years'][str(year)]['has_more_pages'])

    async def test_has_more_pages_false_on_the_last_page(self):
        calls = []
        # total_results == page_size -> exactly 1 page total
        with patch(
            'scopus_mcp.server.client.search_scopus',
            side_effect=recording_fake_search(calls, total_results=SEARCH_RECENT_YEARS_PAGE_SIZE),
        ):
            result = await handle_call_tool(
                'search_recent_years', {'search_content': 'agent system', 'page': 1}
            )

        data = json.loads(result[0].text)
        self.assert_no_year_errors(data)
        for year in expected_years():
            group = data['years'][str(year)]
            self.assertEqual(group['total_pages'], 1)
            self.assertFalse(group['has_more_pages'])

    async def test_one_years_api_failure_does_not_lose_the_other_years(self):
        """Fault injection: one year's request fails (e.g. transient
        network error); the other 3 years must still come back populated,
        with the failed year reporting its error instead of raising and
        losing everything."""
        failing_year = expected_years()[0]

        async def flaky_search(query, count, start, sort):
            year = int(re.search(r'PUBYEAR = (\d+)', query).group(1))
            if year == failing_year:
                raise RuntimeError("simulated transient failure")
            return canned_response(year, total_results=1)

        with patch('scopus_mcp.server.client.search_scopus', side_effect=flaky_search):
            result = await handle_call_tool('search_recent_years', {'search_content': 'agent system'})

        data = json.loads(result[0].text)
        failed_group = data['years'][str(failing_year)]
        self.assertIn('error', failed_group)
        self.assertEqual(failed_group['results'], [])

        for year in expected_years():
            if year == failing_year:
                continue
            group = data['years'][str(year)]
            self.assertNotIn('error', group)
            self.assertEqual(len(group['results']), 1)

    async def test_sort_is_relevancy_not_the_default_coverdate(self):
        calls = []
        with patch('scopus_mcp.server.client.search_scopus', side_effect=recording_fake_search(calls)):
            result = await handle_call_tool('search_recent_years', {'search_content': 'agent system'})

        data = json.loads(result[0].text)
        self.assert_no_year_errors(data)
        for call in calls:
            self.assertEqual(call['sort'], 'relevancy')


if __name__ == '__main__':
    unittest.main()
