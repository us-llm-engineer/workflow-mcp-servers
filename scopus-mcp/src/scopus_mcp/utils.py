from typing import Dict, List, Any, Optional

def clean_search_results(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extracts and normalizes search results from the Scopus Search API response.

    Args:
        data: The raw JSON response from Scopus API.

    Returns:
        A list of simplified dictionaries containing key article metadata.
    """
    if not data or 'search-results' not in data:
        return []
    
    entries = data['search-results'].get('entry', [])
    cleaned_entries = []
    
    for entry in entries:
        cleaned = {
            'scopus_id': entry.get('dc:identifier', '').replace('SCOPUS_ID:', ''),
            'title': entry.get('dc:title'),
            'creator': entry.get('dc:creator'),
            'publication_name': entry.get('prism:publicationName'),
            'cover_date': entry.get('prism:coverDate'),
            'doi': entry.get('prism:doi'),
            'cited_by_count': entry.get('citedby-count'),
            'aggregation_type': entry.get('prism:aggregationType'),
            'url': next((link['@href'] for link in entry.get('link', []) if link.get('@ref') == 'scopus'), None)
        }
        cleaned_entries.append(cleaned)
        
    return cleaned_entries

def clean_search_results_full(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extracts a fuller set of fields from a STANDARD-view Scopus Search API
    response than clean_search_results does -- open-access label, document
    type, source type, affiliations, ISSN/eISSN/ISBN, volume/issue/pages,
    both the Scopus record link and a doi.org link.

    Deliberately does NOT include the abstract text. Confirmed live: Scopus
    Search API's COMPLETE view and every richer Abstract Retrieval view
    (FULL, META_ABS) return 401 AUTHORIZATION_ERROR for API keys without
    elevated/institutional access -- the abstract text itself is not
    obtainable at STANDARD-tier access regardless of which endpoint or
    view is requested, so it is not a matter of asking a different way.

    Args:
        data: The raw JSON response from Scopus's Search API (STANDARD view).

    Returns:
        A list of dictionaries, one per result entry, with every field
        actually present at STANDARD view populated (fields Scopus didn't
        return for that entry, e.g. an ISSN on a book, are None).
    """
    if not data or 'search-results' not in data:
        return []

    entries = data['search-results'].get('entry', [])
    cleaned_entries = []

    for entry in entries:
        doi = entry.get('prism:doi')

        affiliations = [
            {
                'name': a.get('affilname'),
                'city': a.get('affiliation-city'),
                'country': a.get('affiliation-country'),
            }
            for a in (entry.get('affiliation') or [])
        ]

        isbn_field = entry.get('prism:isbn')
        isbn = None
        if isinstance(isbn_field, list) and isbn_field:
            isbn = isbn_field[0].get('$')
        elif isinstance(isbn_field, str):
            isbn = isbn_field

        oa_label_values = (entry.get('freetoreadLabel') or {}).get('value', [])
        open_access_labels = [v.get('$') for v in oa_label_values if isinstance(v, dict) and v.get('$')]

        cleaned_entries.append({
            'scopus_id': entry.get('dc:identifier', '').replace('SCOPUS_ID:', ''),
            'eid': entry.get('eid'),
            'title': entry.get('dc:title'),
            'creator': entry.get('dc:creator'),
            'publication_name': entry.get('prism:publicationName'),
            'cover_date': entry.get('prism:coverDate'),
            'doi': doi,
            'doi_url': f'https://doi.org/{doi}' if doi else None,
            'cited_by_count': entry.get('citedby-count'),
            'aggregation_type': entry.get('prism:aggregationType'),
            'document_type_code': entry.get('subtype'),
            'document_type': entry.get('subtypeDescription'),
            'issn': entry.get('prism:issn'),
            'eissn': entry.get('prism:eIssn'),
            'isbn': isbn,
            'volume': entry.get('prism:volume'),
            'issue': entry.get('prism:issueIdentifier'),
            'page_range': entry.get('prism:pageRange'),
            'affiliations': affiliations,
            'open_access': bool(entry.get('openaccessFlag', False)),
            'open_access_labels': open_access_labels,
            'scopus_url': next((l['@href'] for l in entry.get('link', []) if l.get('@ref') == 'scopus'), None),
        })

    return cleaned_entries


def clean_abstract_details(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts relevant details from the Scopus Abstract Retrieval API response.

    Args:
        data: The raw JSON response from Scopus API.

    Returns:
        A simplified dictionary containing abstract details.
    """
    # The API key can be singular or plural depending on endpoint version/context,
    # though usually 'abstracts-retrieval-response'.
    root = data.get('abstracts-retrieval-response') or data.get('abstract-retrieval-response')
    
    if not root:
        return {}

    coredata = root.get('coredata', {})
    authors_data = root.get('authors', {}).get('author', [])
    
    # Normalize authors to a list even if single author (API inconsistency)
    if isinstance(authors_data, dict):
        authors_data = [authors_data]
        
    authors = []
    for auth in authors_data:
        authors.append({
            'auth_id': auth.get('@auid'),
            'name': auth.get('ce:indexed-name'),
            'surname': auth.get('ce:surname'),
            'initials': auth.get('ce:initials')
        })

    return {
        'scopus_id': coredata.get('dc:identifier', '').replace('SCOPUS_ID:', ''),
        'doi': coredata.get('prism:doi'),
        'title': coredata.get('dc:title'),
        'description': coredata.get('dc:description'), # This is the abstract text
        'publication_name': coredata.get('prism:publicationName'),
        'cover_date': coredata.get('prism:coverDate'),
        'cited_by_count': coredata.get('citedby-count'),
        'authors': authors,
        'url': next((link['@href'] for link in coredata.get('link', []) if link.get('@ref') == 'scopus'), None)
    }

def clean_author_profile(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts details from the Scopus Author Retrieval API response.

    Args:
        data: The raw JSON response from Scopus API.

    Returns:
        A simplified dictionary containing author profile information.
    """
    if not data or 'author-retrieval-response' not in data:
        return {}
    
    root = data['author-retrieval-response']
    core = root.get('coredata', {})
    profile = root.get('author-profile', {})
    
    name_variant = profile.get('preferred-name', {})
    
    return {
        'author_id': core.get('dc:identifier', '').replace('AUTHOR_ID:', ''),
        'orcid': core.get('orcid'),
        'document_count': core.get('document-count'),
        'cited_by_count': core.get('cited-by-count'),
        'citation_count': core.get('citation-count'),
        'name': {
            'surname': name_variant.get('surname'),
            'given_name': name_variant.get('given-name'),
            'initials': name_variant.get('initials')
        },
        'current_affiliation': _extract_affiliation(profile),
        'url': next((link['@href'] for link in core.get('link', []) if link.get('@ref') == 'scopus-author'), None)
    }

def _extract_affiliation(profile: Dict[str, Any]) -> Optional[str]:
    """Helper to extract current affiliation name."""
    affil = profile.get('affiliation-current', {}).get('affiliation', {})
    # Sometimes it's a list if multiple affiliations
    if isinstance(affil, list):
        affil = affil[0] if affil else {}
        
    return affil.get('ip-doc', {}).get('afdispname')
