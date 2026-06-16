#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
CACHE_FILE = Path(__file__).parent / ".cache" / "fetched_articles.json"
OUTPUT_FILE = Path(__file__).parent.parent / "docs" / "articles.json"

# Source: https://www.nlm.nih.gov/services/queries/dental_strategy.html
NLM_DENTAL_JOURNALS = [
    "Acta Odontol Latinoam", "Acta Odontol Scand", "Adv Dent Res", "Am J Dent",
    "Am J Orthod Dentofacial Orthop", "Anesth Prog", "Angle Orthod", "Arch Oral Biol",
    "Atlas Oral Maxillofac Surg Clin North Am", "Aust Dent J", "Aust Endod J",
    "BMC Oral Health", "Br Dent J", "Br J Oral Maxillofac Surg", "Braz Dent J",
    "Braz Oral Res", "Bull Tokyo Dent Coll", "Can J Dent Hyg", "Caries Res",
    "Chin J Dent Res", "Cleft Palate Craniofac J", "Clin Adv Periodontics",
    "Clin Exp Dent Res", "Clin Implant Dent Relat Res", "Clin Oral Implants Res",
    "Clin Oral Investig", "Community Dent Health", "Community Dent Oral Epidemiol",
    "Compend Contin Educ Dent", "Cranio", "Dent Clin North Am", "Dent Mater J",
    "Dent Mater", "Dent Med Probl", "Dent Traumatol", "Dental Press J Orthod",
    "Dentomaxillofac Radiol", "Eur Arch Paediatr Dent", "Eur Endod J",
    "Eur J Dent Educ", "Eur J Oral Sci", "Eur J Orthod", "Eur J Paediatr Dent",
    "Eur J Prosthodont Restor Dent", "Evid Based Dent", "Facial Plast Surg",
    "Gen Dent", "Gerodontology", "Head Face Med", "Hua Xi Kou Qiang Yi Xue Za Zhi",
    "Indian J Dent Res", "Int Dent J", "Int Endod J", "Int J Comput Dent",
    "Int J Dent Hyg", "Int J Esthet Dent", "Int J Implant Dent",
    "Int J Oral Implantol (Berl)", "Int J Oral Maxillofac Implants",
    "Int J Oral Maxillofac Surg", "Int J Oral Sci", "Int J Paediatr Dent",
    "Int J Periodontics Restorative Dent", "Int J Prosthodont", "Int Orthod",
    "J Adhes Dent", "J Am Dent Assoc", "J Appl Oral Sci", "J Can Dent Assoc",
    "J Clin Dent", "J Clin Orthod", "J Clin Pediatr Dent", "J Clin Periodontol",
    "J Contemp Dent Pract", "J Craniofac Surg", "J Craniomaxillofac Surg",
    "J Dent Child (Chic)", "J Dent Educ", "J Dent Hyg", "J Dent Res", "J Dent",
    "J Endod", "J Esthet Restor Dent", "J Evid Based Dent Pract",
    "J Forensic Odontostomatol", "J Hist Dent", "J Indian Prosthodont Soc",
    "J Indian Soc Pedod Prev Dent", "J Int Acad Periodontol", "J Oral Biosci",
    "J Oral Facial Pain Headache", "J Oral Implantol", "J Oral Maxillofac Surg",
    "J Oral Pathol Med", "J Oral Rehabil", "J Oral Sci", "J Orofac Orthop",
    "J Orthod", "J Periodontal Res", "J Periodontol", "J Prosthet Dent",
    "J Prosthodont Res", "J Prosthodont", "J Public Health Dent",
    "J Stomatol Oral Maxillofac Surg", "J Vet Dent", "J World Fed Orthod",
    "JDR Clin Trans Res", "Med Oral Patol Oral Cir Bucal", "Minerva Dent Oral Sci",
    "Mol Oral Microbiol", "Monogr Oral Sci", "Ned Tijdschr Tandheelkd", "Odontology",
    "Oper Dent", "Oral Dis", "Oral Health Prev Dent", "Oral Maxillofac Surg Clin North Am",
    "Oral Maxillofac Surg", "Oral Surg Oral Med Oral Pathol Oral Radiol",
    "Orthod Craniofac Res", "Orthod Fr", "Pediatr Dent", "Periodontol 2000",
    "Prim Dent J", "Prog Orthod", "Quintessence Int", "Shanghai Kou Qiang Yi Xue",
    "Spec Care Dentist", "Stomatologiia (Mosk)", "Stomatologija", "Swiss Dent J",
    "Zhonghua Kou Qiang Yi Xue Za Zhi",
]

QUERY = " OR ".join(f'"{j}"[Journal]' for j in NLM_DENTAL_JOURNALS)

SPECIALTIES = [
    "Implants", "Perio", "Ortho", "Endo",
    "Restorative", "Oral Surgery", "Pediatric", "Public Health", "Other"
]


def ncbi_params(extra: dict) -> dict:
    params = {"api_key": os.getenv("NCBI_API_KEY")} if os.getenv("NCBI_API_KEY") else {}
    params.update(extra)
    return params


def fetch_pmids() -> list[str]:
    data = ncbi_params({
        "db": "pubmed",
        "term": QUERY,
        "reldate": "30",
        "datetype": "pdat",
        "retmax": "3000",
        "sort": "pub_date",
        "retmode": "json",
    })
    # POST avoids 414 URI Too Long when query contains many [Journal] terms
    resp = requests.post(ESEARCH_URL, data=data, timeout=30)
    resp.raise_for_status()
    pmids = resp.json()["esearchresult"]["idlist"]
    print(f"Found {len(pmids)} PMIDs in last 30 days")
    return pmids


def fetch_details(pmids: list[str]) -> list[dict]:
    articles = []
    batch_size = 20
    delay = 0.34 if not os.getenv("NCBI_API_KEY") else 0.11  # respect rate limits

    for i in range(0, len(pmids), batch_size):
        batch = pmids[i:i + batch_size]
        params = ncbi_params({
            "db": "pubmed",
            "id": ",".join(batch),
            "rettype": "xml",
            "retmode": "xml",
        })
        resp = requests.get(EFETCH_URL, params=params, timeout=30)
        resp.raise_for_status()

        root = ET.fromstring(resp.text)
        for article_el in root.findall(".//PubmedArticle"):
            parsed = parse_article(article_el)
            if parsed:
                articles.append(parsed)

        print(f"  Fetched details for batch {i // batch_size + 1} ({len(articles)} articles so far)")
        time.sleep(delay)

    return articles


def parse_article(el: ET.Element) -> dict | None:
    def text(path):
        node = el.find(path)
        return node.text.strip() if node is not None and node.text else ""

    pmid = text(".//PMID")
    title = text(".//ArticleTitle")

    # Collect abstract text (may have multiple AbstractText sections)
    abstract_parts = []
    for ab in el.findall(".//AbstractText"):
        label = ab.get("Label", "")
        part = (ab.text or "").strip()
        if part:
            abstract_parts.append(f"{label}: {part}" if label else part)
    abstract = " ".join(abstract_parts)

    if not abstract:
        return None  # skip articles with no abstract

    journal = text(".//Journal/Title") or text(".//ISOAbbreviation")
    pub_date_el = el.find(".//PubDate")
    year = text(".//PubDate/Year") or text(".//PubDate/MedlineDate")[:4]
    month = text(".//PubDate/Month")
    pub_date = f"{month} {year}".strip() if month else year

    authors = []
    for author in el.findall(".//Author")[:3]:
        last = text.__func__(author, "LastName") if False else (
            (author.find("LastName").text if author.find("LastName") is not None else "") or ""
        )
        if last:
            authors.append(last)

    doi = ""
    for aid in el.findall(".//ArticleId"):
        if aid.get("IdType") == "doi":
            doi = aid.text or ""

    return {
        "pmid": pmid,
        "title": title,
        "abstract": abstract,
        "journal": journal,
        "pub_date": pub_date,
        "authors": authors,
        "doi": doi,
        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    }


def print_summary(articles: list[dict]):
    print(f"\n{'#':<5} {'PMID':<12} {'Journal':<35} Title")
    print("-" * 100)
    for i, a in enumerate(articles, 1):
        title_short = a["title"][:55] + "…" if len(a["title"]) > 55 else a["title"]
        journal_short = a["journal"][:33] + "…" if len(a["journal"]) > 33 else a["journal"]
        print(f"{i:<5} {a['pmid']:<12} {journal_short:<35} {title_short}")
    print(f"\nTotal: {len(articles)} articles with abstracts")


def score_batch(client, batch: list[dict], batch_num: int, total_batches: int) -> list[dict]:
    import anthropic

    articles_text = ""
    for a in batch:
        articles_text += f"\nPMID: {a['pmid']}\nTitle: {a['title']}\nAbstract: {a['abstract'][:800]}\n---\n"

    prompt = f"""Score each dental journal article for a curated dental digest read by practicing clinicians.
Return a JSON array — one object per article — with exactly these fields:
- pmid (string, copy from input)
- clinical_score (integer 0-10)
- novelty_score (integer 0-10)
- specialty (string, one of: {', '.join(SPECIALTIES)})
- summary (string): exactly two plain-English sentences a general dentist would find useful

## CLINICAL SCORE rubric (0–10)
Score how directly and broadly the finding changes real patient care.

9–10: Large RCT or meta-analysis with an immediate, practice-changing recommendation applicable to most dental patients or a broad specialist population.
7–8: Clear clinical guidance for a sizeable patient group; a general dentist or common specialist could act on this finding today.
5–6: Relevant to a specialist subgroup, or provides useful supporting evidence but requires further validation before changing practice.
3–4: In-vitro, animal, or purely mechanistic study; findings cannot yet be applied clinically. Also: epidemiological findings with no actionable implication.
1–2: Basic science or background research with no near-term clinical translation.
0: No clinical relevance.

Applicability penalty: If the finding only applies to a rare condition, highly specialized procedure, or a very narrow patient population, reduce the score by 2.

## NOVELTY SCORE rubric (0–10)
Score how genuinely new and surprising the contribution is — not just whether it uses a new method.

9–10: Paradigm shift — overturns established belief, introduces a wholly new treatment concept, or produces a finding that contradicts what the field expected (e.g., a drug that regenerates permanent teeth reaching a clinical trial milestone).
7–8: A genuinely new technique, material, or biological mechanism not previously described; a surprising result that challenges current understanding.
5–6: Meaningful advancement with clear differentiation from prior work; a new combination of known approaches that produces a non-obvious result.
3–5: Incremental optimization of an existing technique or material. AI/machine learning applied to dental imaging or diagnosis falls here by default unless it achieves a clinically significant breakthrough that no prior system could.
1–2: Replication, confirmation, or minor variation of existing findings.
0: No novel contribution.

Calibration note: Do not reward novelty simply because a paper uses deep learning, LLMs, or AI. These are now routine tools. Score the clinical or scientific insight, not the methodology.

Return only the JSON array, no other text.

Articles:
{articles_text}"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            timeout=60,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        scores = json.loads(raw)
        results = []
        for score in scores:
            pmid = str(score.get("pmid", ""))
            orig = next((a for a in batch if a["pmid"] == pmid), None)
            if orig:
                merged = {**orig, **score}
                merged.pop("abstract", None)
                results.append(merged)
        print(f"  [{batch_num}/{total_batches}] scored {len(results)} articles", flush=True)
        return results
    except Exception as e:
        print(f"  [{batch_num}/{total_batches}] failed: {e}", flush=True)
        return []


def score_articles(articles: list[dict]) -> list[dict]:
    import anthropic
    from concurrent.futures import ThreadPoolExecutor, as_completed

    client = anthropic.Anthropic()
    batch_size = 10
    batches = [articles[i:i + batch_size] for i in range(0, len(articles), batch_size)]
    total = len(batches)
    print(f"  Scoring {len(articles)} articles in {total} batches (10 concurrent)...", flush=True)

    scored = []
    # 10 concurrent workers — well within Haiku rate limits
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(score_batch, client, batch, i + 1, total): i
            for i, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            scored.extend(future.result())

    return scored


def load_journal_metrics() -> dict:
    """Load OpenAlex journal metrics cache. Returns {} if not built yet."""
    metrics_file = CACHE_FILE.parent / "journal_metrics.json"
    if metrics_file.exists():
        return json.loads(metrics_file.read_text())
    return {}


def journal_score(journal_name: str, metrics: dict) -> float:
    """
    Normalize 2yr_mean_citedness to a 0–5 score.
    Cap at citedness=10 so outliers (Periodontology 2000 at 14) don't dominate.
    """
    entry = metrics.get(journal_name, {})
    citedness = entry.get("2yr_citedness", 0) or 0
    return round(min(citedness / 10.0, 1.0) * 5, 2)


def journal_homepage(journal_name: str, metrics: dict) -> str:
    entry = metrics.get(journal_name, {})
    return entry.get("homepage_url", "") or ""


def build_output(scored: list[dict]) -> dict:
    from datetime import datetime, timezone

    metrics = load_journal_metrics()
    if metrics:
        print(f"  Loaded journal metrics for {len(metrics)} journals")

    # Compute journal score and total, then rank
    for a in scored:
        a["journal_score"] = journal_score(a.get("journal", ""), metrics)

    ranked = sorted(
        scored,
        key=lambda a: a.get("clinical_score", 0) + a.get("novelty_score", 0) + a.get("journal_score", 0),
        reverse=True,
    )[:10]

    articles_out = []
    for a in ranked:
        clinical = a.get("clinical_score", 0)
        novelty  = a.get("novelty_score", 0)
        j_score  = a.get("journal_score", 0)
        badges = []
        if clinical >= 7:
            badges.append("Clinical")
        if novelty >= 7:
            badges.append("Novel")

        articles_out.append({
            "pmid":           a["pmid"],
            "title":          a["title"],
            "journal":        a["journal"],
            "journal_url":    journal_homepage(a.get("journal", ""), metrics),
            "pub_date":       a["pub_date"],
            "authors":        a["authors"],
            "doi":            a["doi"],
            "pubmed_url":     a["pubmed_url"],
            "summary":        a.get("summary", ""),
            "specialty":      a.get("specialty", "Other"),
            "clinical_score": clinical,
            "novelty_score":  novelty,
            "journal_score":  j_score,
            "badges":         badges,
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "articles": articles_out,
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch and score dental journal articles")
    parser.add_argument("--fetch-only", action="store_true", help="Fetch from PubMed and print summary, then stop")
    parser.add_argument("--score-only", action="store_true", help="Score cached fetch results and print, then stop")
    parser.add_argument("--output-only", action="store_true", help="Write articles.json from existing scored cache, skip fetch and score")
    args = parser.parse_args()

    CACHE_FILE.parent.mkdir(exist_ok=True)

    if args.output_only:
        scored_cache = CACHE_FILE.with_suffix(".scored.json")
        if not scored_cache.exists():
            print("No scored cache found. Run --score-only first.")
            sys.exit(1)
        scored = json.loads(scored_cache.read_text())
        output = build_output(scored)
        OUTPUT_FILE.write_text(json.dumps(output, indent=2))
        print(f"Wrote {len(output['articles'])} articles to {OUTPUT_FILE}")
        return

    if args.score_only:
        if not CACHE_FILE.exists():
            print("No cache found. Run without --score-only first to fetch articles.")
            sys.exit(1)
        articles = json.loads(CACHE_FILE.read_text())
        print(f"Loaded {len(articles)} cached articles. Scoring with Claude...")
        scored = score_articles(articles)
        print(f"\nScored {len(scored)} articles. Top 10 by score:")
        print(f"\n{'Score':<7} {'Specialty':<15} {'Clinical':<10} {'Novelty':<10} Title")
        print("-" * 90)
        ranked = sorted(scored, key=lambda a: a.get("clinical_score", 0) + a.get("novelty_score", 0), reverse=True)
        for a in ranked[:10]:  # preview top 10
            total = a.get("clinical_score", 0) + a.get("novelty_score", 0)
            title_short = a["title"][:45] + "…" if len(a["title"]) > 45 else a["title"]
            print(f"{total:<7} {a.get('specialty','?'):<15} {a.get('clinical_score',0):<10} {a.get('novelty_score',0):<10} {title_short}")
        print("\nSample summary:", ranked[0].get("summary", "") if ranked else "—")
        # Save scored cache for full run
        CACHE_FILE.with_suffix(".scored.json").write_text(json.dumps(scored, indent=2))
        print(f"\nScored cache saved to {CACHE_FILE.with_suffix('.scored.json')}")
        return

    # Fetch phase
    print("Fetching PMIDs from PubMed...")
    pmids = fetch_pmids()
    print("Fetching article details...")
    articles = fetch_details(pmids)
    CACHE_FILE.write_text(json.dumps(articles, indent=2))
    print_summary(articles)

    if args.fetch_only:
        print(f"\nCache saved to {CACHE_FILE}")
        return

    # Score phase
    print("\nScoring articles with Claude...")
    scored = score_articles(articles)

    # Output phase
    output = build_output(scored)
    OUTPUT_FILE.write_text(json.dumps(output, indent=2))
    print(f"\nWrote {len(output['articles'])} articles to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
