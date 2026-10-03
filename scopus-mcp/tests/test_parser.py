import pytest
from scopus_mcp.utils import (
    clean_search_results,
    clean_search_results_full,
    clean_abstract_details,
    clean_author_profile,
)

def test_clean_search_results_empty():
    assert clean_search_results({}) == []
    assert clean_search_results({'search-results': {}}) == []

def test_clean_search_results_valid():
    data = {
        'search-results': {
            'entry': [
                {
                    'dc:identifier': 'SCOPUS_ID:12345',
                    'dc:title': 'Test Title',
                    'prism:coverDate': '2023-01-01',
                    'citedby-count': '10',
                    'link': [{'@ref': 'scopus', '@href': 'http://example.com'}]
                }
            ]
        }
    }
    cleaned = clean_search_results(data)
    assert len(cleaned) == 1
    assert cleaned[0]['scopus_id'] == '12345'
    assert cleaned[0]['title'] == 'Test Title'
    assert cleaned[0]['url'] == 'http://example.com'

def test_clean_search_results_full_empty():
    assert clean_search_results_full({}) == []
    assert clean_search_results_full({'search-results': {}}) == []


def test_clean_search_results_full_extracts_every_field():
    # Shape confirmed live against the real Scopus Search API (STANDARD view).
    data = {
        'search-results': {
            'entry': [
                {
                    'dc:identifier': 'SCOPUS_ID:85215947767',
                    'eid': '2-s2.0-85215947767',
                    'dc:title': 'A trust model for open multi-agent systems',
                    'dc:creator': 'Zoi L.',
                    'prism:publicationName': 'ACM International Conference Proceeding Series',
                    'prism:isbn': [{'$': '9798400709821'}],
                    'prism:coverDate': '2024-12-27',
                    'prism:doi': '10.1145/3688671.3688785',
                    'citedby-count': '3',
                    'affiliation': [
                        {'affilname': 'Hellenic Open University', 'affiliation-city': 'Patra', 'affiliation-country': 'Greece'}
                    ],
                    'prism:aggregationType': 'Conference Proceeding',
                    'subtype': 'cp',
                    'subtypeDescription': 'Conference Paper',
                    'openaccessFlag': True,
                    'freetoreadLabel': {'value': [{'$': 'All Open Access'}, {'$': 'Gold'}]},
                    'link': [
                        {'@ref': 'scopus', '@href': 'https://www.scopus.com/inward/record.uri?scp=85215947767'},
                        {'@ref': 'self', '@href': 'https://api.elsevier.com/content/abstract/scopus_id/85215947767'},
                    ],
                }
            ]
        }
    }
    cleaned = clean_search_results_full(data)
    assert len(cleaned) == 1
    r = cleaned[0]
    assert r['scopus_id'] == '85215947767'
    assert r['eid'] == '2-s2.0-85215947767'
    assert r['title'] == 'A trust model for open multi-agent systems'
    assert r['creator'] == 'Zoi L.'
    assert r['publication_name'] == 'ACM International Conference Proceeding Series'
    assert r['cover_date'] == '2024-12-27'
    assert r['doi'] == '10.1145/3688671.3688785'
    assert r['doi_url'] == 'https://doi.org/10.1145/3688671.3688785'
    assert r['cited_by_count'] == '3'
    assert r['aggregation_type'] == 'Conference Proceeding'
    assert r['document_type_code'] == 'cp'
    assert r['document_type'] == 'Conference Paper'
    assert r['isbn'] == '9798400709821'
    assert r['issn'] is None
    assert r['affiliations'] == [{'name': 'Hellenic Open University', 'city': 'Patra', 'country': 'Greece'}]
    assert r['open_access'] is True
    assert r['open_access_labels'] == ['All Open Access', 'Gold']
    assert r['scopus_url'] == 'https://www.scopus.com/inward/record.uri?scp=85215947767'


def test_clean_search_results_full_missing_optional_fields_are_none_not_keyerror():
    data = {'search-results': {'entry': [{'dc:identifier': 'SCOPUS_ID:1'}]}}
    r = clean_search_results_full(data)[0]
    assert r['scopus_id'] == '1'
    assert r['doi'] is None
    assert r['doi_url'] is None
    assert r['isbn'] is None
    assert r['issn'] is None
    assert r['affiliations'] == []
    assert r['open_access'] is False
    assert r['open_access_labels'] == []
    assert r['scopus_url'] is None


def test_clean_search_results_full_journal_entry_uses_issn_not_isbn():
    data = {
        'search-results': {
            'entry': [
                {
                    'dc:identifier': 'SCOPUS_ID:2',
                    'prism:issn': '00189448',
                    'prism:eIssn': '15579654',
                    'prism:volume': '70',
                    'prism:issueIdentifier': '3',
                    'prism:pageRange': '1-15',
                    'prism:aggregationType': 'Journal',
                    'subtype': 'ar',
                    'subtypeDescription': 'Article',
                }
            ]
        }
    }
    r = clean_search_results_full(data)[0]
    assert r['issn'] == '00189448'
    assert r['eissn'] == '15579654'
    assert r['volume'] == '70'
    assert r['issue'] == '3'
    assert r['page_range'] == '1-15'
    assert r['isbn'] is None
    assert r['aggregation_type'] == 'Journal'
    assert r['document_type'] == 'Article'


def test_clean_abstract_details():
    data = {
        'abstracts-retrieval-response': {
            'coredata': {
                'dc:identifier': 'SCOPUS_ID:999',
                'dc:title': 'Abstract Title',
                'dc:description': 'This is an abstract.'
            },
            'authors': {
                'author': [
                    {'@auid': '111', 'ce:indexed-name': 'Doe J.'}
                ]
            }
        }
    }
    cleaned = clean_abstract_details(data)
    assert cleaned['scopus_id'] == '999'
    assert cleaned['title'] == 'Abstract Title'
    assert len(cleaned['authors']) == 1
    assert cleaned['authors'][0]['auth_id'] == '111'
