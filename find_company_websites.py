import re
import csv
import time
import requests
from ddgs import DDGS
from urllib.parse import urlparse
from rapidfuzz import fuzz

HEADERS = {"User-Agent": "CompanyWebsiteFinder/1.0"}

SKIP_DOMAINS = [
    "linkedin.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "reddit.com",
    "wikipedia.org", "crunchbase.com", "glassdoor.com",
    "bloomberg.com", "reuters.com", "forbes.com",
    "indeed.com", "yelp.com", "zoominfo.com", "dnb.com",
    "trustpilot.com", "bbb.org", "owler.com",
]

SUFFIXES = {"inc", "ltd", "llc", "corp", "co", "plc", "gmbh", "ag",
            "sa", "bv", "nv", "ab", "oy", "as", "limited", "company"}


def clean_name(company):
    return " ".join(w for w in company.split() if w.lower().rstrip(".") not in SUFFIXES).strip()


def get_root_url(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.netloc else url


def is_blocked(url):
    return any(s in url for s in SKIP_DOMAINS)


# ── Source 1: Clearbit autocomplete ──────────────────────────────
def find_via_clearbit(company):
    queries = list(dict.fromkeys([clean_name(company), company]))
    first_word = clean_name(company).split()[0].lower() if clean_name(company) else ""

    for query in queries:
        try:
            data = requests.get(
                "https://autocomplete.clearbit.com/v1/companies/suggest",
                params={"query": query}, timeout=8, headers=HEADERS
            ).json()

            candidates = []
            for r in data:
                name = r.get("name", "")
                domain = r.get("domain", "")
                if not domain or is_blocked(domain):
                    continue
                dr = domain.split(".")[0].lower()
                tld = domain.split(".")[-1].lower()

                score = max(
                    fuzz.token_set_ratio(company.lower(), name.lower()),
                    fuzz.token_sort_ratio(company.lower(), name.lower()),
                )
                if score < 65:
                    continue

                bonus = (15 if first_word and dr.startswith(first_word[:4]) else 0) + \
                        (10 if tld in ("com", "net", "org") else 0)
                candidates.append((score + bonus, len(domain), domain))

            candidates.sort(key=lambda x: (-x[0], x[1]))
            if candidates:
                return f"https://{candidates[0][2]}"
        except Exception:
            pass
    return None


# ── Source 2: DuckDuckGo search (with retries) ───────────────────
def find_via_ddg(company):
    query = clean_name(company)
    search_queries = [
        f'"{company}" official website',
        f'{company} official site',
        f'{query} company homepage',
    ]
    for attempt in range(3):
        try:
            for q in search_queries:
                results = DDGS().text(q, max_results=10)
                for r in results or []:
                    url = r.get("href", "")
                    title = r.get("title", "")
                    body = r.get("body", "")
                    if not url or is_blocked(url):
                        continue
                    score = max(
                        fuzz.partial_ratio(query.lower(), title.lower()),
                        fuzz.partial_ratio(query.lower(), body.lower()),
                    )
                    if score >= 70:
                        return get_root_url(url)
            return None
        except Exception:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    return None


# ── Source 3: Domain name guess ───────────────────────────────────
def find_via_domain_guess(company):
    query = clean_name(company)
    slug = re.sub(r"[^a-z0-9]", "", query.lower().replace(" ", ""))
    slug_dash = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]", "-", query.lower())).strip("-")

    for url in [f"https://{slug}.com", f"https://{slug_dash}.com"]:
        if not slug:
            continue
        try:
            r = requests.head(url, timeout=5, allow_redirects=True, headers=HEADERS)
            if r.status_code < 400:
                return url
        except Exception:
            pass
    return None


def find_website(company):
    for fn, source in [
        (find_via_clearbit, "Clearbit"),
        (find_via_ddg,      "DuckDuckGo"),
        (find_via_domain_guess, "Domain guess"),
    ]:
        result = fn(company)
        if result:
            return result, source
    return "Not found", "—"


# ── Main ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    companies = [
        "Stripe",
        "OpenAI",
        "Shopify",
        "Airbnb",
        "Tesla",
        "Infosys",
        "Tata Motors",
        "Microsoft",
    ]

    results = []
    for company in companies:
        print(f"Searching: {company} ...", end=" ", flush=True)
        website, source = find_website(company)
        print(f"{website}  [{source}]")
        results.append({"Company": company, "Website": website, "Source": source})
        time.sleep(1)

    with open("company_websites.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Company", "Website", "Source"])
        writer.writeheader()
        writer.writerows(results)

    print("\nSaved to company_websites.csv")
