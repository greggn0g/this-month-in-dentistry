#!/usr/bin/env python3
"""
Fetch recent dental-literature articles from PubMed, pre-filter them cheaply,
score the survivors with Claude, and write docs/articles.json.

Pipeline (each stage is a separate CLI flag so CI can run them independently):

    --fetch-only    PubMed esearch + efetch  ->  .cache/fetched_articles.json
    --score-only    pre-filter + Claude      ->  .cache/fetched_articles.scored.json
    --output-only   rank + weight            ->  docs/articles.json

Design rule: this script fails loudly. It will never publish an empty or
near-empty articles.json, because a silent "success" that wipes the site is
worse than a red build.
"""
import argparse
import json
import math
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
CACHE_FILE = Path(__file__).parent / ".cache" / "fetched_articles.json"
METRICS_FILE = Path(__file__).parent / "data" / "journal_metrics.json"
OUTPUT_FILE = Path(__file__).parent.parent / "docs" / "articles.json"

MODEL = "claude-opus-5"

# How many articles survive the pre-filter and get sent to Claude.
SCORE_LIMIT = 450
# Finalists that go through the comparative re-rank.
FINALIST_LIMIT = 30
# Technique and case papers held open in the finalist pool. The composite
# ranks them below trial designs by construction (level 4-5 evidence, lower
# citedness journals), so without a reserved slot the comparative pass never
# sees one and none can ever be published.
TECHNIQUE_FINALISTS = 8
# How many make the final published list.
PUBLISH_LIMIT = 12
# Refuse to publish if fewer than this many articles survived scoring.
MIN_PUBLISHABLE = 5
# Refuse to publish if more than this share of scoring batches failed.
MAX_BATCH_FAILURE_RATE = 0.2
# Refuse to continue if more than this share of PubMed batches failed.
MAX_FETCH_FAILURE_RATE = 0.1

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

# Article types that are never worth a slot in a monthly digest. Case reports
# are deliberately NOT here: a well-documented technique case from a clinical
# journal is often more useful to a practising clinician than another cohort.
EXCLUDED_PUB_TYPES = {
    "Autobiography", "Bibliography", "Biography", "Comment",
    "Congress", "Editorial", "Historical Article", "Interview", "Lecture",
    "Letter", "News", "Newspaper Article", "Portrait", "Published Erratum",
    "Retracted Publication", "Retraction of Publication", "Video-Audio Media",
}

# Journals whose whole point is showing clinicians how a case was handled.
# Case reports and technique papers from these are a feature, not noise.
TECHNIQUE_JOURNAL_PATTERN = re.compile(
    r"(clinical advances in periodont"
    r"|periodontics\s*(?:&|and)\s*restorative"
    r"|esthetic dentistry"
    r"|esthetic and restorative"
    r"|compendium of continuing education"
    r"|quintessence international"
    r"|international journal of computerized dentistry"
    r"|journal of oral implantology"
    r"|practical procedures)",
    re.IGNORECASE,
)

# The month's most common filler: a model is trained on images, a sensitivity
# is reported, nothing about patient care changes. These are demoted at screen
# time and capped in the rubric — not excluded, because a genuine diagnostic
# breakthrough should still be able to earn a slot.
AI_ACCURACY_PATTERN = re.compile(
    r"\b(deep learning|machine learning|artificial intelligence|neural network|"
    r"convolutional|\bCNN\b|\bYOLO\b|transformer|large language model|\bLLM\b|"
    r"ChatGPT|automated detection|automatic segmentation)\b",
    re.IGNORECASE,
)

CLINICAL_OUTCOME_PATTERN = re.compile(
    r"\b(survival|success rate|attachment (?:gain|loss)|bone (?:gain|level|loss)|"
    r"pain|quality of life|patient-reported|complication|failure rate|"
    r"randomi[sz]ed|treatment outcome|healing|recurrence|mortality)\b",
    re.IGNORECASE,
)

# Titles that promise a reproducible "how it was done".
TECHNIQUE_PATTERN = re.compile(
    r"\b(technique|case series|case report|clinical report|surgical approach|"
    r"step-by-step|novel approach|modified \w+ (?:flap|technique|approach)|"
    r"workflow|protocol|management of|reconstruction|rehabilitation of)\b",
    re.IGNORECASE,
)

# Study designs that carry the most clinical weight.
PRIORITY_PUB_TYPES = {
    "Randomized Controlled Trial": 4,
    "Meta-Analysis": 4,
    "Systematic Review": 3,
    "Practice Guideline": 3,
    "Multicenter Study": 2,
    "Clinical Trial": 2,
    "Controlled Clinical Trial": 2,
    "Observational Study": 1,
    "Comparative Study": 1,
}

# Designs admitted to scoring regardless of journal prestige. A well-run trial
# in an unfashionable journal is exactly the paper this digest exists to find.
GUARANTEED_PUB_TYPES = {
    "Randomized Controlled Trial",
    "Meta-Analysis",
    "Systematic Review",
    "Practice Guideline",
    "Controlled Clinical Trial",
    "Multicenter Study",
}

# Signals that a study was run in people rather than in a dish or an animal.
HUMAN_STUDY_PATTERN = re.compile(
    r"\b(patients?|participants?|subjects?|volunteers?|adults?|children|"
    r"randomi[sz]ed|cohort|cross-sectional|case-control|follow-up|"
    r"clinical trial|in vivo|recruited|enrolled)\b",
    re.IGNORECASE,
)

NON_HUMAN_PATTERN = re.compile(
    r"\b(in vitro|ex vivo|rats?|mice|murine|rabbits?|beagles?|canine|porcine|"
    r"bovine|cadaver|extracted teeth|cell line|osteoblast|fibroblast culture|"
    r"finite element|simulat\w+|phantom)\b",
    re.IGNORECASE,
)

# Sample size, e.g. "n = 1,240" or "240 patients were enrolled".
SAMPLE_SIZE_PATTERN = re.compile(
    r"\bn\s*=\s*([0-9][0-9,]{1,6})\b|\b([0-9][0-9,]{1,6})\s+(?:patients|participants|subjects)\b",
    re.IGNORECASE,
)

# Greg is a periodontist — perio and implant work gets weighted up, but nothing
# is excluded, so genuinely big findings elsewhere still surface.
FOCUS_SPECIALTIES = {"Perio", "Implants"}
FOCUS_PATTERN = re.compile(
    r"\b("
    r"periodont\w*|peri-?implant\w*|gingiv\w*|mucogingival|"
    r"implant\w*|osseointegrat\w*|edentulous|"
    r"guided bone regenerat\w*|bone graft\w*|sinus (?:lift|augmentation)|"
    r"soft tissue graft\w*|connective tissue graft|free gingival graft|"
    r"recession|attachment loss|probing depth|furcation|"
    r"alveolar ridge|ridge preservation|socket preservation|"
    r"scaling and root planing|subgingival|supragingival|calculus|"
    r"biofilm|oral microbiome|periodontitis|gingivitis|mucositis"
    r")\b",
    re.IGNORECASE,
)


def ncbi_params(extra: dict) -> dict:
    params = {"api_key": os.getenv("NCBI_API_KEY")} if os.getenv("NCBI_API_KEY") else {}
    params.update(extra)
    return params


def fetch_pmids() -> list[str]:
    """Page through esearch so a busy month isn't silently truncated."""
    pmids: list[str] = []
    page_size = 5000
    retstart = 0

    while True:
        data = ncbi_params({
            "db": "pubmed",
            "term": QUERY,
            "reldate": "30",
            "datetype": "pdat",
            "retmax": str(page_size),
            "retstart": str(retstart),
            "sort": "pub_date",
            "retmode": "json",
        })
        # POST avoids 414 URI Too Long when query contains many [Journal] terms
        resp = requests.post(ESEARCH_URL, data=data, timeout=30)
        resp.raise_for_status()
        result = resp.json()["esearchresult"]
        batch = result["idlist"]
        pmids.extend(batch)

        total = int(result.get("count", len(pmids)))
        retstart += len(batch)
        if not batch or retstart >= total:
            print(f"Found {len(pmids)} PMIDs in last 30 days (PubMed reports {total})")
            return pmids
        time.sleep(0.34 if not os.getenv("NCBI_API_KEY") else 0.11)


def fetch_batch_details(batch: list[str], delay: float) -> list[dict]:
    """
    Fetch one batch of PMIDs. NCBI returns transient 400s and 429s under load,
    so retry with backoff. Uses POST because efetch's documented limit for GET
    is ~200 IDs and long query strings are what provoke the 400s.
    """
    data = ncbi_params({
        "db": "pubmed",
        "id": ",".join(batch),
        "rettype": "xml",
        "retmode": "xml",
    })

    last_error = None
    for attempt in range(4):
        try:
            resp = requests.post(EFETCH_URL, data=data, timeout=45)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            return [
                parsed for el in root.findall(".//PubmedArticle")
                if (parsed := parse_article(el))
            ]
        except (requests.RequestException, ET.ParseError) as e:
            last_error = e
            if attempt < 3:
                time.sleep(delay + 2 ** attempt + random.uniform(0, 1))

    raise RuntimeError(f"efetch failed after 4 attempts: {last_error}")


def fetch_details(pmids: list[str]) -> list[dict]:
    """
    Fetch article detail for every PMID. A batch that keeps failing is skipped
    rather than aborting the run — losing 20 of 2,700 articles is survivable,
    losing the whole month is not. Too many skips still fails the run.
    """
    articles = []
    batch_size = 20
    delay = 0.34 if not os.getenv("NCBI_API_KEY") else 0.11  # respect rate limits
    batches = [pmids[i:i + batch_size] for i in range(0, len(pmids), batch_size)]
    skipped = 0

    for n, batch in enumerate(batches, 1):
        try:
            articles.extend(fetch_batch_details(batch, delay))
        except RuntimeError as e:
            skipped += 1
            print(f"  Batch {n}/{len(batches)} SKIPPED: {e}", flush=True)
        if n % 10 == 0 or n == len(batches):
            print(f"  Fetched {n}/{len(batches)} batches ({len(articles)} articles)", flush=True)
        time.sleep(delay)

    if skipped:
        rate = skipped / len(batches)
        print(f"  WARNING: skipped {skipped}/{len(batches)} batches ({rate:.1%})")
        if rate > MAX_FETCH_FAILURE_RATE:
            sys.exit(
                f"{rate:.0%} of PubMed batches failed, above the "
                f"{MAX_FETCH_FAILURE_RATE:.0%} threshold. The month would be "
                "incomplete; refusing to continue."
            )

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
    year = text(".//PubDate/Year") or text(".//PubDate/MedlineDate")[:4]
    month = text(".//PubDate/Month")
    pub_date = f"{month} {year}".strip() if month else year

    authors = []
    for author in el.findall(".//Author")[:3]:
        last_el = author.find("LastName")
        if last_el is not None and last_el.text:
            authors.append(last_el.text.strip())

    doi = ""
    for aid in el.findall(".//ArticleId"):
        if aid.get("IdType") == "doi":
            doi = aid.text or ""

    pub_types = [pt.text.strip() for pt in el.findall(".//PublicationType") if pt.text]
    languages = [lang.text.strip() for lang in el.findall(".//Language") if lang.text]

    return {
        "pmid": pmid,
        "title": title,
        "abstract": abstract,
        "journal": journal,
        "pub_date": pub_date,
        "authors": authors,
        "doi": doi,
        "pub_types": pub_types,
        "languages": languages,
        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    }


# ---------------------------------------------------------------- pre-filter


def is_focus_article(article: dict) -> bool:
    """True if the title or abstract reads as perio / implant work."""
    haystack = f"{article.get('title', '')} {article.get('abstract', '')}"
    return bool(FOCUS_PATTERN.search(haystack))


def is_ai_accuracy_paper(article: dict) -> bool:
    """
    True for "we trained a model and report its accuracy" papers that never
    touch a clinical outcome. Common, and almost never actionable.
    """
    text = f"{article.get('title', '')} {article.get('abstract', '')}"
    if not AI_ACCURACY_PATTERN.search(text):
        return False
    return not CLINICAL_OUTCOME_PATTERN.search(text)


def is_technique_article(article: dict) -> bool:
    """
    A case report or technique paper worth a clinician's time — either it comes
    from a journal built for that kind of writing, or the title reads as a
    reproducible method rather than a one-off curiosity.
    """
    from_technique_journal = bool(TECHNIQUE_JOURNAL_PATTERN.search(article.get("journal", "")))
    is_case = "Case Reports" in article.get("pub_types", [])
    reads_as_method = bool(TECHNIQUE_PATTERN.search(article.get("title", "")))
    return (is_case and (from_technique_journal or reads_as_method)) or (
        from_technique_journal and reads_as_method
    )


def looks_like_human_study(article: dict) -> bool:
    """Cheap heuristic used only for screening; the model makes the real call."""
    text = f"{article.get('title', '')} {article.get('abstract', '')}"
    if NON_HUMAN_PATTERN.search(text) and not re.search(r"\bpatients?\b", text, re.I):
        return False
    return bool(HUMAN_STUDY_PATTERN.search(text))


def detect_sample_size(article: dict) -> int | None:
    """Largest reported n in the abstract, as a rough scale signal."""
    text = article.get("abstract", "") or ""
    sizes = []
    for m in SAMPLE_SIZE_PATTERN.finditer(text):
        raw = m.group(1) or m.group(2)
        try:
            sizes.append(int(raw.replace(",", "")))
        except ValueError:
            continue
    return max(sizes) if sizes else None


def screen_score(article: dict, metrics: dict) -> float:
    """
    Cheap, deterministic pre-score used only to decide what Claude looks at.
    Not shown to the user and not part of the published ranking.
    """
    score = journal_score(article.get("journal", ""), metrics)

    pub_types = set(article.get("pub_types", []))
    design_bonus = max(
        (weight for name, weight in PRIORITY_PUB_TYPES.items() if name in pub_types),
        default=0,
    )
    score += design_bonus

    if is_focus_article(article):
        score += 2
    if is_technique_article(article):
        score += 2.5
    if is_ai_accuracy_paper(article):
        score -= 4
    if looks_like_human_study(article):
        score += 1.5
    size = detect_sample_size(article)
    if size and size >= 200:
        score += 1

    return score


def prefilter(articles: list[dict], metrics: dict, limit: int = SCORE_LIMIT) -> list[dict]:
    """
    Narrow the month's haul before spending model tokens on it.

    Ranking by screen score alone would let a strong trial in a low-citedness
    journal fall off the bottom, which is exactly the paper worth surfacing.
    So two tiers are admitted unconditionally first:

      1. Strong designs — RCTs, meta-analyses, systematic reviews, guidelines.
      2. Anything in the reader's own field that looks like a human study.

    Whatever budget is left is filled by screen score.
    """
    eligible = []
    dropped_type = 0
    dropped_lang = 0

    for a in articles:
        if EXCLUDED_PUB_TYPES & set(a.get("pub_types", [])):
            dropped_type += 1
            continue
        languages = a.get("languages") or ["eng"]
        if "eng" not in languages:
            dropped_lang += 1
            continue
        eligible.append(a)

    # Only strong study designs are guaranteed. An earlier version also
    # guaranteed every own-field human study, which matched 538 of 2,218
    # papers — it overran the budget and squeezed the screen score out
    # entirely. Own-field work is favoured through screen_score instead.
    guaranteed, remainder = [], []
    for a in eligible:
        strong_design = bool(GUARANTEED_PUB_TYPES & set(a.get("pub_types", [])))
        technique = is_technique_article(a)
        (guaranteed if strong_design or technique else remainder).append(a)

    guaranteed.sort(key=lambda a: screen_score(a, metrics), reverse=True)
    remainder.sort(key=lambda a: screen_score(a, metrics), reverse=True)

    selected = guaranteed[:limit]
    selected += remainder[:max(0, limit - len(selected))]

    focus_count = sum(1 for a in selected if is_focus_article(a))
    print(
        f"  Pre-filter: {len(articles)} fetched -> {len(eligible)} eligible "
        f"(dropped {dropped_type} by article type, {dropped_lang} non-English)"
    )
    print(
        f"  Admitted {len(selected)} for scoring: "
        f"{min(len(guaranteed), limit)} guaranteed (strong study design), "
        f"{len(selected) - min(len(guaranteed), limit)} by screen score; "
        f"{focus_count} perio/implant"
    )
    if len(guaranteed) > limit:
        print(f"  WARNING: {len(guaranteed) - limit} guaranteed-tier articles exceeded the budget and were cut")
    elif len(eligible) > len(selected):
        print(f"  Note: {len(eligible) - len(selected)} lower-scoring articles were not sent for scoring")

    return selected


# -------------------------------------------------------------------- scoring


SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pmid": {"type": "string"},
                    "clinical_score": {"type": "integer"},
                    "novelty_score": {"type": "integer"},
                    "evidence_level": {"type": "integer"},
                    "human_study": {"type": "boolean"},
                    "specialty": {"type": "string", "enum": SPECIALTIES},
                    "summary": {"type": "string"},
                    "bottom_line": {"type": "string"},
                },
                "required": [
                    "pmid", "clinical_score", "novelty_score", "evidence_level",
                    "human_study", "specialty", "summary", "bottom_line",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scores"],
    "additionalProperties": False,
}

RERANK_SCHEMA = {
    "type": "object",
    "properties": {
        "ranking": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pmid": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["pmid", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ranking"],
    "additionalProperties": False,
}

RUBRIC = f"""Score each dental journal article for a monthly research digest read by a practicing periodontist.

## CLINICAL SCORE rubric (0-10)
Score how directly and broadly the finding changes real patient care.

9-10: Large RCT or meta-analysis with an immediate, practice-changing recommendation applicable to most dental patients or a broad specialist population.
7-8: Clear clinical guidance for a sizeable patient group; a clinician could act on this finding today.
5-6: Relevant to a specialist subgroup, or useful supporting evidence that needs further validation before changing practice.
3-4: In-vitro, animal, or purely mechanistic study; cannot yet be applied clinically. Also: epidemiological findings with no actionable implication.
1-2: Basic science or background research with no near-term clinical translation.
0: No clinical relevance.

Applicability penalty: if the finding only applies to a rare condition, highly specialized procedure, or very narrow patient population, reduce the score by 2.

Case reports and technique papers: do NOT penalise these for lacking a control group — that is not what they are for. Judge them on whether a competent clinician could actually adopt the technique: is the method described in enough detail to reproduce, does it address a problem clinicians genuinely encounter, and is the result better than the conventional approach? A clearly described, reproducible technique for a common problem deserves 7-8. A one-off curiosity, an exotic presentation with no transferable method, or a technique needing equipment almost nobody has deserves 2-3.

## NOVELTY SCORE rubric (0-10)
Score how genuinely new and surprising the contribution is, not just whether it uses a new method.

9-10: Paradigm shift. Overturns established belief, introduces a wholly new treatment concept, or contradicts what the field expected.
7-8: A genuinely new technique, material, or biological mechanism not previously described; a surprising result that challenges current understanding.
5-6: Meaningful advancement with clear differentiation from prior work; a new combination of known approaches producing a non-obvious result.
3-5: Incremental optimization of an existing technique or material. AI or machine learning applied to dental imaging or diagnosis falls here by default unless it achieves a clinically significant breakthrough no prior system could.
1-2: Replication, confirmation, or minor variation of existing findings.
0: No novel contribution.

Calibration: do not reward novelty simply because a paper uses deep learning, LLMs, or AI. Those are routine tools now. Score the clinical or scientific insight, not the methodology.

Diagnostic-accuracy AI papers: a study that trains a model on images and reports sensitivity, specificity, AUC or Dice score, without measuring any effect on treatment decisions or patient outcomes, must score at most 3 for clinical relevance and at most 3 for novelty — however good the metrics are. Dozens are published every month and they do not change practice. Score higher ONLY if the model was tested prospectively in clinical use, changed documented treatment decisions, or outperformed experienced clinicians on a task they actually struggle with.

## EVIDENCE LEVEL (1-5, lower is stronger)
Judge the study design from the abstract, not from the journal or the authors' claims.

1: Systematic review or meta-analysis of randomised trials.
2: Individual randomised controlled trial.
3: Non-randomised controlled study, prospective cohort, or large registry analysis.
4: Case-control, cross-sectional, retrospective series, or narrative review.
5: In-vitro, animal, cadaver, finite-element or purely computational work; expert opinion.

If the abstract does not let you tell, choose the weaker (higher) level. Do not infer a randomised design from the word "trial" alone.

Evidence level records the study design; it is not a verdict on the paper's worth. A case report is level 4 or 5 and may still be the most useful thing a clinician reads this month. Score it honestly here and let the clinical score carry its value.

## HUMAN STUDY
true only if the data come from living human subjects or their clinical records. In-vitro, animal, cadaver, extracted-tooth and simulation work is false. A systematic review of human trials is true.

## SPECIALTY
Exactly one of: {', '.join(SPECIALTIES)}. Use "Perio" for periodontal disease, soft tissue, and regenerative work; "Implants" for implant placement, peri-implant disease, and osseointegration.

## SUMMARY
Exactly two plain-English sentences. The first states what the study found, with the actual effect size or number where the abstract gives one. The second states what it means for a clinician. No preamble, no hedging boilerplate, no restating the title.

## BOTTOM LINE
One short sentence naming what a clinician should do differently, if anything. If the honest answer is that nothing changes yet, say so plainly — that is a useful result, not a failure."""


def clamp(value, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return low


def score_batch(client, batch: list[dict], batch_num: int, total_batches: int) -> list[dict] | None:
    """
    Score one batch. Returns the scored articles, or None if the batch failed
    after retries — the caller counts Nones to decide whether the run is sound.

    Authentication errors are re-raised immediately: a bad API key fails every
    batch, and burning through hundreds of them to discover that wastes minutes
    and buries the real error in noise.
    """
    import anthropic

    articles_text = ""
    for a in batch:
        pub_types = ", ".join(a.get("pub_types", [])) or "not specified"
        articles_text += (
            f"\nPMID: {a['pmid']}\n"
            f"Title: {a['title']}\n"
            f"Journal: {a['journal']}\n"
            f"Publication type: {pub_types}\n"
            f"Abstract: {a['abstract'][:1200]}\n---\n"
        )

    prompt = f"{RUBRIC}\n\nScore every article below. Return one object per article.\n\nArticles:\n{articles_text}"

    last_error = None
    for attempt in range(3):
        try:
            message = client.messages.create(
                model=MODEL,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": "medium",
                    "format": {"type": "json_schema", "schema": SCORE_SCHEMA},
                },
                messages=[{"role": "user", "content": prompt}],
            )

            if message.stop_reason == "refusal":
                raise RuntimeError(f"model refused: {message.stop_details}")

            raw = next(b.text for b in message.content if b.type == "text")
            scores = json.loads(raw)["scores"]

            results = []
            by_pmid = {a["pmid"]: a for a in batch}
            for score in scores:
                orig = by_pmid.get(str(score.get("pmid", "")))
                if not orig:
                    continue
                merged = {**orig, **score}
                merged["clinical_score"] = clamp(merged.get("clinical_score"), 0, 10)
                merged["novelty_score"] = clamp(merged.get("novelty_score"), 0, 10)
                merged["evidence_level"] = clamp(merged.get("evidence_level"), 1, 5)
                merged["sample_size"] = detect_sample_size(orig)
                merged.pop("abstract", None)
                results.append(merged)

            print(f"  [{batch_num}/{total_batches}] scored {len(results)}/{len(batch)}", flush=True)
            return results

        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            raise
        except Exception as e:  # noqa: BLE001 - retry anything else
            last_error = e
            if attempt < 2:
                time.sleep(2 ** attempt + random.uniform(0, 1))

    print(f"  [{batch_num}/{total_batches}] FAILED after 3 attempts: {last_error}", flush=True)
    return None


def score_articles(articles: list[dict]) -> list[dict]:
    """
    Score every article, then refuse to continue if too much of the run failed.
    Partial results are worse than none here: a half-scored month would quietly
    publish whichever articles happened to succeed.
    """
    import anthropic
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Scoring cannot run.")

    client = anthropic.Anthropic(max_retries=3)
    batch_size = 10
    batches = [articles[i:i + batch_size] for i in range(0, len(articles), batch_size)]
    total = len(batches)
    print(f"  Scoring {len(articles)} articles with {MODEL} in {total} batches...", flush=True)

    scored: list[dict] = []
    failures = 0

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(score_batch, client, batch, i + 1, total): i
            for i, batch in enumerate(batches)
        }
        try:
            for future in as_completed(futures):
                result = future.result()
                if result is None:
                    failures += 1
                else:
                    scored.extend(result)
        except anthropic.AuthenticationError as e:
            executor.shutdown(wait=False, cancel_futures=True)
            sys.exit(
                f"\nANTHROPIC_API_KEY is invalid or expired ({e}).\n"
                "Create a new key at https://console.anthropic.com, then update the\n"
                "ANTHROPIC_API_KEY secret under Settings > Secrets and variables > Actions."
            )
        except anthropic.PermissionDeniedError as e:
            executor.shutdown(wait=False, cancel_futures=True)
            sys.exit(f"\nANTHROPIC_API_KEY lacks permission for {MODEL}: {e}")

    failure_rate = failures / total if total else 1.0
    print(f"\n  Scored {len(scored)} articles; {failures}/{total} batches failed ({failure_rate:.0%})")

    if not scored:
        sys.exit("No articles were scored successfully. Refusing to continue.")
    if failure_rate > MAX_BATCH_FAILURE_RATE:
        sys.exit(
            f"{failure_rate:.0%} of scoring batches failed, above the "
            f"{MAX_BATCH_FAILURE_RATE:.0%} threshold. Refusing to publish a "
            "partially-scored month."
        )

    return scored


def rerank_finalists(scored: list[dict]) -> list[dict]:
    """
    Scores from independent batches aren't calibrated against each other — an 8
    from one batch isn't necessarily an 8 from another, because the model never
    saw them side by side. This pass shows the top candidates together and asks
    for a single ranked list, which is what actually decides the published order.

    If the pass fails, the composite ordering stands. It improves the result; it
    is not load-bearing.
    """
    import anthropic

    metrics = load_journal_metrics()
    for a in scored:
        a["journal_score"] = journal_score(a.get("journal", ""), metrics)
        a["focus_bonus"] = focus_bonus(a)

    ordered = sorted(scored, key=composite, reverse=True)
    finalists = ordered[:FINALIST_LIMIT]

    # Put the best technique/case work in front of the model even though the
    # composite ranks it lower, and let the comparison decide on merit.
    seen_ids = {a["pmid"] for a in finalists}
    extras = [a for a in ordered
              if is_technique_article(a) and a["pmid"] not in seen_ids][:TECHNIQUE_FINALISTS]
    finalists += extras
    if extras:
        print(f"  Added {len(extras)} technique/case candidates to the comparison")

    if len(finalists) <= PUBLISH_LIMIT:
        return ordered

    lines = ""
    for a in finalists:
        lines += (
            f"\nPMID: {a['pmid']}\n"
            f"Title: {a['title']}\n"
            f"Journal: {a['journal']}\n"
            f"Specialty: {a.get('specialty')} | Evidence level: {a.get('evidence_level')} "
            f"| Human study: {a.get('human_study')} | n: {a.get('sample_size') or 'not stated'}"
            f"{' | TECHNIQUE/CASE PAPER' if is_technique_article(a) else ''}\n"
            f"Summary: {a.get('summary', '')}\n"
            f"Bottom line: {a.get('bottom_line', '')}\n---\n"
        )

    prompt = f"""You are assembling the final issue of a monthly research digest for a practising periodontist.

Below are {len(finalists)} candidate papers that already passed screening and scoring. Candidates marked TECHNIQUE/CASE PAPER are surgical technique or case work, included deliberately — see the note on them below. They were scored in separate batches, so their numbers are not calibrated against each other. Your job is to compare them directly and choose the {PUBLISH_LIMIT} most worth this reader's attention, in order.

Rank on what would genuinely change or inform practice:
- Strength of evidence and study design carry more weight than novelty or journal name.
- Prefer human studies with real clinical endpoints over bench work, however elegant.
- Periodontics and implant papers matter most to this reader, but a major finding in another specialty still belongs in the issue. Do not fill every slot with perio.
- Prefer one strong paper on a topic over three similar ones. If several cover the same ground, keep the best and drop the rest.
- This reader specifically wants surgical technique and well-documented case work alongside the trials. If any candidates are reproducible technique or case papers of real quality, include the best two or three; judge them on whether the technique is adoptable, not on sample size.
- Demote papers whose abstract promises more than the design can support.
- Exclude AI or machine-learning papers that only report model accuracy metrics on images. This reader does not want them. Include one only if it demonstrably changes a treatment decision or patient outcome.

Return exactly {PUBLISH_LIMIT} entries, best first, each with the pmid and a one-line reason for its position.

Candidates:
{lines}"""

    try:
        client = anthropic.Anthropic(max_retries=3)
        message = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "high",
                "format": {"type": "json_schema", "schema": RERANK_SCHEMA},
            },
            messages=[{"role": "user", "content": prompt}],
        )
        if message.stop_reason == "refusal":
            raise RuntimeError(f"model refused: {message.stop_details}")

        raw = next(b.text for b in message.content if b.type == "text")
        ranking = json.loads(raw)["ranking"]
    except Exception as e:  # noqa: BLE001 - optional improvement, never fatal
        print(f"  Re-rank pass failed ({e}); falling back to composite order.", flush=True)
        return ordered

    by_pmid = {a["pmid"]: a for a in finalists}
    chosen, seen = [], set()
    for position, item in enumerate(ranking):
        a = by_pmid.get(str(item.get("pmid", "")))
        if not a or a["pmid"] in seen:
            continue
        seen.add(a["pmid"])
        a["rank_position"] = position
        a["rank_reason"] = item.get("reason", "")
        chosen.append(a)

    if len(chosen) < MIN_PUBLISHABLE:
        print(f"  Re-rank returned only {len(chosen)} usable entries; using composite order.")
        return ordered

    rest = [a for a in ordered if a["pmid"] not in seen]
    print(f"  Re-ranked {len(finalists)} finalists into a calibrated top {len(chosen)}")
    return chosen + rest


# ------------------------------------------------------------------- ranking


def load_journal_metrics() -> dict:
    """Load the OpenAlex journal metrics cache. Returns {} if not built yet."""
    if METRICS_FILE.exists():
        return json.loads(METRICS_FILE.read_text())
    return {}


def journal_score(journal_name: str, metrics: dict) -> float:
    """
    Normalize 2yr_mean_citedness to a 0-5 score.
    Cap at citedness=10 so outliers (Periodontology 2000 at 14) don't dominate.
    """
    entry = metrics.get(journal_name, {})
    citedness = entry.get("2yr_citedness", 0) or 0
    return round(min(citedness / 10.0, 1.0) * 5, 2)


def journal_homepage(journal_name: str, metrics: dict) -> str:
    entry = metrics.get(journal_name, {})
    return entry.get("homepage_url", "") or ""


def focus_bonus(article: dict) -> float:
    """
    Weight perio and implant work up without excluding anything else.
    A high-scoring ortho or endo paper can still outrank a mediocre perio one.
    """
    if article.get("specialty") in FOCUS_SPECIALTIES:
        return 3.0
    if is_focus_article(article):
        return 1.5
    return 0.0


# Study design counts for as much as journal prestige. A level-1 review in a
# mid-tier journal should beat a level-5 bench study in a famous one.
EVIDENCE_WEIGHT = {1: 3.0, 2: 2.5, 3: 1.5, 4: 0.5, 5: 0.0}


def evidence_bonus(article: dict) -> float:
    bonus = EVIDENCE_WEIGHT.get(article.get("evidence_level"), 0.0)
    if article.get("human_study"):
        bonus += 1.0
    size = article.get("sample_size")
    if size and size >= 500:
        bonus += 0.5
    return bonus


def technique_bonus(article: dict) -> float:
    """
    Case reports sit at evidence level 4-5 by definition, so the evidence
    weighting alone would bury every technique paper. This restores enough
    ground that a genuinely useful one can still make the issue on the
    strength of its clinical score.
    """
    return 2.0 if is_technique_article(article) else 0.0


def composite(article: dict) -> float:
    return (
        -4.0 * is_ai_accuracy_paper(article)
        + article.get("clinical_score", 0)
        + article.get("novelty_score", 0)
        + article.get("journal_score", 0)
        + article.get("focus_bonus", 0)
        + evidence_bonus(article)
        + technique_bonus(article)
    )


def build_output(scored: list[dict], articles_scanned: int | None = None) -> dict:
    from datetime import datetime, timezone

    metrics = load_journal_metrics()
    if metrics:
        print(f"  Loaded journal metrics for {len(metrics)} journals")
    else:
        print("  WARNING: no journal metrics found; journal quality will score 0")

    for a in scored:
        a["journal_score"] = journal_score(a.get("journal", ""), metrics)
        a["focus_bonus"] = focus_bonus(a)

    # The comparative pass, when it ran, decides the order; composite is the
    # tiebreak for anything it didn't rank.
    ranked = sorted(
        scored,
        key=lambda a: (a.get("rank_position", math.inf), -composite(a)),
    )[:PUBLISH_LIMIT]

    articles_out = []
    for a in ranked:
        clinical = a.get("clinical_score", 0)
        novelty = a.get("novelty_score", 0)
        badges = []
        if clinical >= 7:
            badges.append("Clinical")
        if novelty >= 7:
            badges.append("Novel")
        if a.get("evidence_level") in (1, 2):
            badges.append("Strong evidence")
        if is_technique_article(a):
            badges.append("Technique")
        if a.get("specialty") in FOCUS_SPECIALTIES:
            badges.append("Your field")

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
            "bottom_line":    a.get("bottom_line", ""),
            "specialty":      a.get("specialty", "Other"),
            "clinical_score": clinical,
            "novelty_score":  novelty,
            "journal_score":  a.get("journal_score", 0),
            "evidence_level": a.get("evidence_level"),
            "human_study":    a.get("human_study"),
            "sample_size":    a.get("sample_size"),
            "study_types":    [t for t in a.get("pub_types", []) if t in PRIORITY_PUB_TYPES],
            "is_technique":   is_technique_article(a),
            "badges":         badges,
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "journals_scanned": len(NLM_DENTAL_JOURNALS),
        "articles_scanned": articles_scanned if articles_scanned is not None else len(scored),
        "articles_scored": len(scored),
        "articles": articles_out,
    }


def write_output(output: dict):
    """Last line of defence: never overwrite a good articles.json with nothing."""
    count = len(output["articles"])
    if count < MIN_PUBLISHABLE:
        sys.exit(
            f"Only {count} article(s) would be published, below the minimum of "
            f"{MIN_PUBLISHABLE}. Leaving {OUTPUT_FILE.name} untouched."
        )
    OUTPUT_FILE.write_text(json.dumps(output, indent=2))
    print(f"\nWrote {count} articles to {OUTPUT_FILE}")


# ---------------------------------------------------------------------- CLI


def print_summary(articles: list[dict]):
    print(f"\n{'#':<5} {'PMID':<12} {'Journal':<35} Title")
    print("-" * 100)
    for i, a in enumerate(articles, 1):
        title_short = a["title"][:55] + "…" if len(a["title"]) > 55 else a["title"]
        journal_short = a["journal"][:33] + "…" if len(a["journal"]) > 33 else a["journal"]
        print(f"{i:<5} {a['pmid']:<12} {journal_short:<35} {title_short}")
    print(f"\nTotal: {len(articles)} articles with abstracts")


def print_ranking(scored: list[dict]):
    metrics = load_journal_metrics()
    for a in scored:
        a.setdefault("journal_score", journal_score(a.get("journal", ""), metrics))
        a.setdefault("focus_bonus", focus_bonus(a))

    ranked = sorted(scored, key=composite, reverse=True)
    print(f"\n{'Total':<8} {'Specialty':<15} {'Clin':<6} {'Nov':<6} {'Jrnl':<6} Title")
    print("-" * 100)
    for a in ranked[:PUBLISH_LIMIT]:
        title_short = a["title"][:45] + "…" if len(a["title"]) > 45 else a["title"]
        print(f"{composite(a):<8.1f} {a.get('specialty','?'):<15} {a['clinical_score']:<6} "
              f"{a['novelty_score']:<6} {a['journal_score']:<6} {title_short}")
    if ranked:
        print("\nSample summary:", ranked[0].get("summary", ""))


def main():
    parser = argparse.ArgumentParser(description="Fetch and score dental journal articles")
    parser.add_argument("--fetch-only", action="store_true", help="Fetch from PubMed and print summary, then stop")
    parser.add_argument("--score-only", action="store_true", help="Pre-filter and score cached fetch results, then stop")
    parser.add_argument("--output-only", action="store_true", help="Write articles.json from the scored cache")
    args = parser.parse_args()

    CACHE_FILE.parent.mkdir(exist_ok=True)
    scored_cache = CACHE_FILE.with_suffix(".scored.json")

    if args.output_only:
        if not scored_cache.exists():
            sys.exit("No scored cache found. Run --score-only first.")
        scored = json.loads(scored_cache.read_text())
        if not scored:
            sys.exit("Scored cache is empty. Refusing to write articles.json.")
        fetched = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else scored
        write_output(build_output(scored, articles_scanned=len(fetched)))
        return

    if args.score_only:
        if not CACHE_FILE.exists():
            sys.exit("No cache found. Run --fetch-only first.")
        articles = json.loads(CACHE_FILE.read_text())
        print(f"Loaded {len(articles)} cached articles.")
        candidates = prefilter(articles, load_journal_metrics())
        scored = score_articles(candidates)
        scored = rerank_finalists(scored)
        print_ranking(scored)
        scored_cache.write_text(json.dumps(scored, indent=2))
        print(f"\nScored cache saved to {scored_cache}")
        return

    # Full run
    print("Fetching PMIDs from PubMed...")
    pmids = fetch_pmids()
    print("Fetching article details...")
    articles = fetch_details(pmids)
    CACHE_FILE.write_text(json.dumps(articles, indent=2))
    print_summary(articles)

    if args.fetch_only:
        print(f"\nCache saved to {CACHE_FILE}")
        return

    print("\nScoring articles with Claude...")
    candidates = prefilter(articles, load_journal_metrics())
    scored = score_articles(candidates)
    scored = rerank_finalists(scored)
    scored_cache.write_text(json.dumps(scored, indent=2))
    write_output(build_output(scored, articles_scanned=len(articles)))


if __name__ == "__main__":
    main()
