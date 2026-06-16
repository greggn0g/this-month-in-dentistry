#!/usr/bin/env python3
"""
Build a journal metrics cache from OpenAlex, using full journal names
extracted from our fetched PubMed articles for reliable matching.

Run: python3 scripts/build_journal_metrics.py
"""
import json
import time
import requests
from pathlib import Path

FETCH_CACHE  = Path(__file__).parent / ".cache" / "fetched_articles.json"
CACHE_FILE   = Path(__file__).parent / ".cache" / "journal_metrics.json"
OPENALEX_URL = "https://api.openalex.org/sources"
HEADERS      = {"User-Agent": "this-month-in-dentistry/1.0 (mailto:contact@example.com)"}


def lookup_by_name(full_name: str) -> dict | None:
    """Exact-title search in OpenAlex."""
    resp = requests.get(
        OPENALEX_URL,
        params={
            "filter": f"display_name.search:{full_name},type:journal",
            "per_page": 5,
            "select": "id,display_name,issn_l,issn,summary_stats,homepage_url",
        },
        headers=HEADERS,
        timeout=15,
    )
    if not resp.ok:
        return None
    results = resp.json().get("results", [])
    if not results:
        return None

    # Pick the result whose name most closely matches (case-insensitive)
    name_lower = full_name.lower()
    best = None
    for r in results:
        if r.get("display_name", "").lower() == name_lower:
            best = r
            break
    if best is None:
        best = results[0]  # fall back to top result

    stats = best.get("summary_stats", {})
    return {
        "openalex_id":   best.get("id", ""),
        "name":          best.get("display_name", ""),
        "issn_l":        best.get("issn_l", ""),
        "issns":         best.get("issn", []),
        "2yr_citedness": stats.get("2yr_mean_citedness") or 0,
        "h_index":       stats.get("h_index") or 0,
        "homepage_url":  best.get("homepage_url") or "",
    }


def main():
    if not FETCH_CACHE.exists():
        print("Run fetch_articles.py --fetch-only first.")
        return

    articles = json.loads(FETCH_CACHE.read_text())
    # Unique full journal names from our actual fetched data
    journal_names = sorted(set(a["journal"] for a in articles if a.get("journal")))
    print(f"Found {len(journal_names)} unique journal names in fetched data")

    # Load existing cache
    existing = {}
    if CACHE_FILE.exists():
        existing = json.loads(CACHE_FILE.read_text())
        print(f"Resuming — {len(existing)} journals already cached")

    metrics = dict(existing)
    new_count = 0

    for i, name in enumerate(journal_names):
        if name in metrics:
            continue

        result = lookup_by_name(name)
        if result and result["2yr_citedness"] > 0:
            metrics[name] = result
            new_count += 1
            print(f"  [{i+1}/{len(journal_names)}] ✓ {name[:60]:<60} → {result['2yr_citedness']:.2f} citedness")
        elif result:
            metrics[name] = result
            new_count += 1
            print(f"  [{i+1}/{len(journal_names)}] ~ {name[:60]:<60} → matched '{result['name']}' (no citedness)")
        else:
            metrics[name] = {"openalex_id": "", "name": "", "issn_l": "", "issns": [],
                             "2yr_citedness": 0, "h_index": 0}
            print(f"  [{i+1}/{len(journal_names)}] ✗ {name[:60]}")

        CACHE_FILE.write_text(json.dumps(metrics, indent=2))
        time.sleep(0.12)

    print(f"\nDone. {new_count} new journals fetched.")

    # Summary
    found = [(k, v) for k, v in metrics.items() if v.get("2yr_citedness", 0) > 0]
    ranked = sorted(found, key=lambda x: x[1]["2yr_citedness"], reverse=True)
    print(f"\nTop 15 journals by 2yr citedness ({len(found)} journals with data):")
    for name, m in ranked[:15]:
        print(f"  {m['2yr_citedness']:>6.2f}  {name[:65]}")


if __name__ == "__main__":
    main()
