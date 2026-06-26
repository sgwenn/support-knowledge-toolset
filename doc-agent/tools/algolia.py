import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

ALGOLIA_APP_ID = os.environ.get("ALGOLIA_APP_ID", "EOIG7V0A2O")


def _query_one(query: str) -> list[dict]:
    api_key = os.environ.get("ALGOLIA_API_KEY")
    if not api_key:
        raise RuntimeError("ALGOLIA_API_KEY environment variable is not set")
    try:
        resp = httpx.get(
            "https://docsearch.datadoghq.com/1/indexes/datadog/query",
            params={"query": query, "hitsPerPage": 3},
            headers={
                "X-Algolia-Application-Id": ALGOLIA_APP_ID,
                "X-Algolia-API-Key": api_key,
            },
            timeout=5.0,
        )
        if resp.status_code == 200:
            return [
                {
                    "title": hit.get("title", ""),
                    "url": hit.get("url", ""),
                    "relevance": f"Matched query: '{query}'",
                    "key_excerpt": hit.get("content", hit.get("description", ""))[:400],
                }
                for hit in resp.json().get("hits", [])
            ]
    except Exception as e:
        print(f"Algolia search failed for '{query}': {e}")
    return []


def search_docs(queries: list[str]) -> list[dict]:
    targets = queries[:3]
    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
        futures = {ex.submit(_query_one, q): q for q in targets}
        results = []
        for future in as_completed(futures):
            results.extend(future.result())
    return results
