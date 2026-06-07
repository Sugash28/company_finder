import ssl
import socket
import json
import time
import csv
import io
import re
import requests
import streamlit as st
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from ddgs import DDGS
from urllib.parse import urlparse, quote_plus
from rapidfuzz import fuzz

st.set_page_config(page_title="Company Website Finder", page_icon="🔍", layout="centered")
st.title("🔍 Company Website Finder")
st.markdown("Paste company names below (one per line), then click **Find Websites**.")

# ── Constants ─────────────────────────────────────────────────────
SKIP_DOMAINS = {
    # Social media
    "linkedin.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "reddit.com",
    # News / finance
    "bloomberg.com", "reuters.com", "forbes.com", "cnbc.com",
    "businessinsider.com", "techcrunch.com", "wsj.com",
    # Business directories — the biggest source of wrong results
    "globaldatabase.com", "kompass.com", "europages.com",
    "yellowpages.com", "whitepages.com", "superpages.com",
    "manta.com", "bbb.org", "bizapedia.com", "dnb.com",
    "zoominfo.com", "owler.com", "pitchbook.com",
    "crunchbase.com", "opencorporates.com", "infobel.com",
    "thomasnet.com", "kellysearch.com", "hotfrog.com",
    "companieshouse.gov.uk", "bizeurope.com", "europages.co.uk",
    "companies.com", "businessdirectory.com", "cylex.com",
    "brownbook.net", "bigsight.jp", "switchboard.com",
    # Job sites
    "glassdoor.com", "indeed.com", "monster.com", "ziprecruiter.com",
    # Review/listing sites
    "trustpilot.com", "tripadvisor.com", "yelp.com",
    "booking.com", "expedia.com",
    # PR wires
    "prnewswire.com", "businesswire.com", "globenewswire.com",
    # Reference
    "wikipedia.org", "sec.gov",
    # App stores
    "play.google.com", "apps.apple.com",
    # Patent / IP / research databases
    "patsnap.com", "discovery.patsnap.com", "lens.org",
    "patents.google.com", "espacenet.com", "orbit.com",
    "clarivate.com", "derwent.com",
}
SUFFIXES = {
    "inc", "ltd", "llc", "corp", "co", "plc", "gmbh", "ag",
    "sa", "bv", "nv", "ab", "oy", "as", "limited", "company",
    "pvt", "holdings", "group", "technologies", "solutions",
    "services", "international", "global",
    # without-dot forms of common dotted abbreviations
    "spa", "srl", "sas", "snc", "sapa", "kk", "kft", "sro",
}
EXTRA_TLDS = ["com", "net", "org", "io", "co", "ai", "app"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

EXCLUSIONS = "-linkedin -crunchbase -facebook -yelp -zoominfo -glassdoor -indeed -wikipedia"

# Matches trailing dotted legal forms: S.P.A., S.r.l., N.V., B.V., etc.
_DOTTED_SUFFIX_RE = re.compile(r'\s+([A-Za-z]\.){2,}[A-Za-z]?\.*\s*$')


# ── Helpers ───────────────────────────────────────────────────────
def clean_name(company: str) -> str:
    name = company.strip()
    # Strip dotted legal suffixes like S.P.A. / S.r.l. / N.V. before dot removal
    name = _DOTTED_SUFFIX_RE.sub("", name).strip()
    name = name.replace("&", "and").replace("+", "plus")
    name = re.sub(r"[^\w\s]", " ", name)
    words = name.split()
    cleaned = [w for w in words if w.lower().rstrip(".,") not in SUFFIXES]
    return " ".join(cleaned).strip() or company.strip()


def acronym(company: str) -> str:
    words = clean_name(company).split()
    return "".join(w[0] for w in words if w).lower() if len(words) > 1 else ""


def get_root_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.netloc else url


def normalize_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def is_blocked(url: str) -> bool:
    return any(s in url for s in SKIP_DOMAINS)


def name_score(company: str, text: str) -> int:
    q = clean_name(company).lower()
    t = text.lower()
    return max(
        fuzz.partial_ratio(q, t),
        fuzz.token_set_ratio(q, t),
        fuzz.token_sort_ratio(q, t),
    )


def domain_sim(company: str, domain: str) -> int:
    slug = re.sub(r"[^a-z0-9]", "", clean_name(company).lower())
    base = domain.split(".")[0].lower()
    return int(fuzz.ratio(slug, base) * 0.40)


# ── SSL certificate ───────────────────────────────────────────────
def ssl_org_score(company: str, domain: str) -> int:
    try:
        ctx  = ssl.create_default_context()
        conn = ctx.wrap_socket(socket.socket(), server_hostname=domain)
        conn.settimeout(5)
        conn.connect((domain, 443))
        cert = conn.getpeercert()
        conn.close()
        for field in cert.get("subject", []):
            for key, val in field:
                if key == "organizationName":
                    return name_score(company, val)
    except Exception:
        pass
    return 0


# ── Content validation ────────────────────────────────────────────
def validate_url(company: str, url: str) -> int:
    try:
        r = requests.get(url, timeout=7, headers=HEADERS, allow_redirects=True)
        if r.status_code >= 400:
            return 0
        html  = r.text[:25000]

        # 1. JSON-LD structured data — most reliable
        for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE):
            try:
                data  = json.loads(block.strip())
                items = data if isinstance(data, list) else [data]
                for item in items:
                    nm = item.get("name", "") or item.get("legalName", "")
                    if not nm:
                        continue
                    # exact match → 100, near-exact → 98, fuzzy → scored
                    if nm.lower() == clean_name(company).lower():
                        return 100
                    if name_score(company, nm) >= 85:
                        return 98
                    if name_score(company, nm) >= 70:
                        return 90
            except Exception:
                pass

        # 2. SSL certificate
        domain = normalize_domain(url)
        if ssl_org_score(company, domain) >= 75:
            return 95

        # 3. Page signals — exact match check first, then fuzzy
        soup   = BeautifulSoup(html, "html.parser")
        title  = soup.title.string.strip() if soup.title and soup.title.string else ""
        og_tag = soup.find("meta", property="og:site_name")
        og     = og_tag["content"].strip() if og_tag and og_tag.get("content") else ""
        desc_t = soup.find("meta", attrs={"name": "description"})
        desc   = desc_t["content"].strip() if desc_t and desc_t.get("content") else ""
        h1_t   = soup.find("h1")
        h1     = h1_t.get_text(strip=True) if h1_t else ""
        copy_m = re.search(r'©\s*(?:\d{4}[–\-]?\d{0,4})?\s*([^<\n]{2,80})', html)
        copy   = copy_m.group(1).strip() if copy_m else ""

        signals    = [title, og, desc, h1, copy]
        page_text  = " ".join(signals).lower()

        # Exact full name match → highest confidence
        if company.lower() in page_text:
            return 96
        # Exact cleaned name match
        if clean_name(company).lower() in page_text:
            return 92

        return max((name_score(company, s) for s in signals), default=0)
    except Exception:
        return 0


# ── Source functions — each returns list[(url, match_score)] ──────

def _parse_bing(html: str, company: str, limit: int = 5):
    soup = BeautifulSoup(html, "html.parser")
    out  = []
    for a in soup.select("li.b_algo h2 a")[:limit]:
        href  = a.get("href", "")
        title = a.get_text()
        if not href or is_blocked(href):
            continue
        sc = name_score(company, title)
        if sc >= 45:
            out.append((get_root_url(href), sc))
    return out


def source_bing(company: str):
    cleaned = clean_name(company)
    queries = [
        f'"{company}" official website {EXCLUSIONS}',
        f'"{cleaned}" official website {EXCLUSIONS}',
        f'{cleaned} official site {EXCLUSIONS}',
    ]
    results = []
    seen = set()
    for q in queries:
        try:
            r = requests.get(f"https://www.bing.com/search?q={quote_plus(q)}",
                             headers=HEADERS, timeout=10)
            for url, sc in _parse_bing(r.text, company):
                d = normalize_domain(url)
                if d not in seen:
                    seen.add(d)
                    results.append((url, sc))
        except Exception:
            pass
    return results


def source_yahoo(company: str):
    cleaned = clean_name(company)
    queries = [
        f'"{company}" official website {EXCLUSIONS}',
        f'"{cleaned}" official website {EXCLUSIONS}',
    ]
    seen, results = set(), []
    for q in queries:
        try:
            r = requests.get(
                f"https://search.yahoo.com/search?p={quote_plus(q)}",
                headers=HEADERS, timeout=10
            )
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("div.algo h3 a, div.compTitle a")[:5]:
                href  = a.get("href", "")
                title = a.get_text()
                if not href or is_blocked(href) or "yahoo.com" in href:
                    continue
                sc = name_score(company, title)
                if sc >= 45:
                    d = normalize_domain(href)
                    if d not in seen:
                        seen.add(d)
                        results.append((get_root_url(href), sc))
        except Exception:
            pass
    return results


def source_ddg(company: str):
    cleaned = clean_name(company)
    queries = [
        f'"{company}" official website {EXCLUSIONS}',
        f'"{cleaned}" official website {EXCLUSIONS}',
        f'{cleaned} company homepage',
    ]
    for attempt in range(3):
        try:
            results = []
            for q in queries:
                for r in DDGS().text(q, max_results=5) or []:
                    url = r.get("href", "")
                    if not url or is_blocked(url):
                        continue
                    sc = max(
                        name_score(company, r.get("title", "")),
                        name_score(company, r.get("body", "")),
                    )
                    if sc >= 45:
                        results.append((get_root_url(url), sc))
            return results
        except Exception:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    return []


def source_ddg_instant(company: str):
    try:
        data = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": company, "format": "json", "no_redirect": "1", "no_html": "1"},
            headers=HEADERS, timeout=8
        ).json()
        results = []
        for item in data.get("Infobox", {}).get("content", []):
            if item.get("label", "").lower() in ("official website", "website"):
                url = item.get("value", "")
                if url and not is_blocked(url):
                    results.append((get_root_url(url), 90))
        heading = data.get("Heading", "")
        abstract_url = data.get("AbstractURL", "")
        if abstract_url and not is_blocked(abstract_url):
            sc = name_score(company, heading)
            if sc >= 55:
                results.append((get_root_url(abstract_url), sc))
        return results
    except Exception:
        return []


def source_sec_edgar(company: str):
    """SEC EDGAR — 100% accurate for US public companies."""
    cleaned = clean_name(company)
    try:
        # Try full name first, then cleaned name
        data = None
        for search_term in [company, cleaned]:
            resp = requests.get(
                "https://efts.sec.gov/LATEST/search-index?q=%22" + quote_plus(search_term) + "%22&dateRange=custom&startdt=2020-01-01&forms=10-K",
                headers=HEADERS, timeout=8
            ).json()
            if resp.get("hits", {}).get("hits"):
                data = resp
                break
        if data is None:
            data = {}
        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            # try company search endpoint
            r2 = requests.get(
                "https://www.sec.gov/cgi-bin/browse-edgar",
                params={"company": company, "action": "getcompany",
                        "type": "10-K", "dateb": "", "owner": "include",
                        "count": "5", "search_text": "", "output": "atom"},
                headers=HEADERS, timeout=8
            )
            matches = re.findall(r'<company-name>([^<]+)</company-name>.*?<website>([^<]+)</website>', r2.text, re.DOTALL)
            for cname, url in matches:
                if name_score(company, cname) >= 65 and url and not is_blocked(url):
                    return [(get_root_url(url), 90)]
            return []
        for hit in hits[:3]:
            src = hit.get("_source", {})
            cname = src.get("entity_name", "")
            if name_score(company, cname) < 60:
                continue
            # Get business address/website from company facts
            cik = src.get("file_num", "").replace("0-", "").lstrip("0")
            if cik:
                facts = requests.get(
                    f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json",
                    headers=HEADERS, timeout=8
                ).json()
                website = facts.get("website", "") or facts.get("businessAddress", {}).get("stateOrCountry", "")
                if website and not is_blocked(website):
                    if not website.startswith("http"):
                        website = "https://" + website
                    return [(get_root_url(website), 90)]
    except Exception:
        pass
    return []


def source_gleif(company: str):
    """GLEIF LEI database — legally verified global company registry."""
    try:
        data = requests.get(
            "https://api.gleif.org/api/v1/fuzzycompletions",
            params={"field": "entity.legalName", "q": company, "page[size]": 5},
            headers=HEADERS, timeout=10
        ).json()
        for item in data.get("data", []):
            legal_name = item.get("attributes", {}).get("value", "")
            if name_score(company, legal_name) < 60:
                continue
            lei = item.get("relationships", {}).get("lei-records", {}).get("data", [{}])
            if not lei:
                continue
            lei_id = lei[0].get("id", "") if isinstance(lei, list) else lei.get("id", "")
            if not lei_id:
                continue
            record = requests.get(
                f"https://api.gleif.org/api/v1/lei-records/{lei_id}",
                headers=HEADERS, timeout=8
            ).json()
            attrs = record.get("data", {}).get("attributes", {})
            url   = attrs.get("entity", {}).get("registeredAs", "")
            # GLEIF doesn't always store website — try entity name as domain guess
            if not url:
                slug = re.sub(r"[^a-z0-9]", "", legal_name.lower().replace(" ", ""))
                candidate = f"https://{slug}.com"
                try:
                    r = requests.head(candidate, timeout=4, allow_redirects=True, headers=HEADERS)
                    if r.status_code < 400:
                        url = candidate
                except Exception:
                    pass
            if url and not url.startswith("http"):
                url = "https://" + url
            if url and not is_blocked(url):
                return [(get_root_url(url), 85)]
    except Exception:
        pass
    return []


def source_wikidata(company: str):
    try:
        search = requests.get("https://www.wikidata.org/w/api.php", headers=HEADERS, params={
            "action": "wbsearchentities", "search": company,
            "language": "en", "type": "item", "format": "json", "limit": 5
        }, timeout=10).json()
        for entity in search.get("search", []):
            label = entity.get("label", "")
            if name_score(company, label) < 60:
                continue
            eid    = entity["id"]
            claims = requests.get("https://www.wikidata.org/w/api.php", headers=HEADERS, params={
                "action": "wbgetentities", "ids": eid,
                "props": "claims", "format": "json"
            }, timeout=10).json().get("entities", {}).get(eid, {}).get("claims", {})
            if "P856" in claims:
                url = claims["P856"][0]["mainsnak"]["datavalue"]["value"]
                if url and not is_blocked(url):
                    return [(get_root_url(url), 90)]
    except Exception:
        pass
    return []


def source_wikipedia(company: str):
    try:
        search = requests.get("https://en.wikipedia.org/w/api.php", headers=HEADERS, params={
            "action": "query", "list": "search", "srsearch": company,
            "format": "json", "srlimit": 3
        }, timeout=8).json()
        for result in search.get("query", {}).get("search", []):
            title = result["title"]
            if name_score(company, title) < 55:
                continue
            content = requests.get("https://en.wikipedia.org/w/api.php", headers=HEADERS, params={
                "action": "query", "titles": title, "prop": "revisions",
                "rvprop": "content", "rvslots": "main", "format": "json"
            }, timeout=8).json()
            for page in content.get("query", {}).get("pages", {}).values():
                text  = page.get("revisions", [{}])[0].get("slots", {}).get("main", {}).get("*", "")
                match = re.search(r'\|\s*website\s*=\s*(?:\{\{URL\|)?([^\s|}\]\n]+)', text, re.IGNORECASE)
                if match:
                    url = match.group(1).strip().strip("{}")
                    if not url.startswith("http"):
                        url = "https://" + url
                    url = get_root_url(url)
                    if url and not is_blocked(url):
                        return [(url, 90)]
    except Exception:
        pass
    return []


def source_clearbit(company: str):
    cleaned    = clean_name(company)
    # Full name first for best match, then cleaned, then acronym as fallback
    queries    = list(dict.fromkeys([company, cleaned, acronym(company)]))
    first_word = cleaned.split()[0].lower() if cleaned else ""
    results    = []
    for query in queries:
        try:
            data = requests.get(
                "https://autocomplete.clearbit.com/v1/companies/suggest",
                params={"query": query}, timeout=8
            ).json()
            for r in data:
                name   = r.get("name", "")
                domain = r.get("domain", "")
                if not domain or is_blocked(domain):
                    continue
                dr  = domain.split(".")[0].lower()
                tld = domain.split(".")[-1].lower()
                sc  = name_score(company, name)
                if sc < 55:
                    continue
                bonus = (15 if first_word and dr.startswith(first_word[:4]) else 0) + \
                        (10 if tld in ("com", "net", "org") else 0)
                results.append((f"https://{domain}", sc + bonus))
            if results:
                break
        except Exception:
            pass
    return sorted(results, key=lambda x: -x[1])[:3]


def source_linkedin_bing(company: str):
    try:
        q = f"{company} site:linkedin.com/company"
        r = requests.get(f"https://www.bing.com/search?q={quote_plus(q)}",
                         headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        for result in soup.select("li.b_algo"):
            snippet = result.get_text(" ", strip=True)
            m = re.search(r'(?:website|web)[:\s]+([a-z0-9][-a-z0-9.]+\.[a-z]{2,})', snippet, re.IGNORECASE)
            if m:
                url = "https://" + m.group(1).strip().rstrip(".")
                if not is_blocked(url):
                    return [(url, 80)]
    except Exception:
        pass
    return []


def source_crtsh(company: str):
    query = clean_name(company)
    slug  = re.sub(r"[^a-z0-9]", "", query.lower().replace(" ", ""))
    if not slug or len(slug) < 3:
        return []
    try:
        data = requests.get(
            "https://crt.sh/", params={"q": f"%{slug}%", "output": "json"},
            headers=HEADERS, timeout=10
        ).json()
        candidates = {}
        for cert in data[:100]:
            for raw in cert.get("name_value", "").split("\n"):
                domain = raw.strip().lstrip("*.")
                if not domain or is_blocked(domain):
                    continue
                base = domain.split(".")[0].lower()
                if slug not in base:
                    continue
                sim = fuzz.ratio(slug, base)
                if sim > candidates.get(domain, 0):
                    candidates[domain] = sim
        if not candidates:
            return []
        best = max(candidates, key=lambda d: candidates[d])
        if candidates[best] >= 70:
            return [(f"https://{best}", candidates[best])]
    except Exception:
        pass
    return []


def source_domain_guess(company: str):
    query     = clean_name(company)
    slug      = re.sub(r"[^a-z0-9]", "", query.lower().replace(" ", ""))
    slug_dash = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]", "-", query.lower())).strip("-")
    acr       = acronym(company)
    first     = query.split()[0].lower() if query else ""

    candidates = []
    if slug:
        candidates.append(f"https://{slug}.com")
    if slug_dash and slug_dash != slug:
        candidates.append(f"https://{slug_dash}.com")
    for tld in EXTRA_TLDS[1:]:
        candidates.append(f"https://{slug}.{tld}")
    if first and first != slug:
        candidates.append(f"https://{first}.com")
    if acr and len(acr) >= 2:
        candidates.append(f"https://{acr}.com")

    results = []
    for url in candidates:
        try:
            r = requests.head(url, timeout=4, allow_redirects=True, headers=HEADERS)
            if r.status_code < 400:
                results.append((url, 50))
        except Exception:
            pass
    return results


# ── Source registry ───────────────────────────────────────────────
SOURCES = [
    (source_sec_edgar,     "SEC EDGAR",   65),
    (source_gleif,         "GLEIF",       60),
    (source_wikidata,      "Wikidata",    55),
    (source_wikipedia,     "Wikipedia",   50),
    (source_ddg_instant,   "DDG Instant", 45),
    (source_bing,          "Bing",        45),
    (source_yahoo,         "Yahoo",       45),
    (source_clearbit,      "Clearbit",    40),
    (source_linkedin_bing, "LinkedIn",    35),
    (source_ddg,           "DuckDuckGo",  30),
    (source_crtsh,         "crt.sh",      25),
    (source_domain_guess,  "Domain guess",15),
]


def _collect_votes(company: str, search_name: str) -> dict:
    """Run all sources IN PARALLEL, score domains against original company."""
    votes: dict = {}

    def run_source(fn, source_name, base_weight):
        try:
            return source_name, base_weight, fn(search_name)
        except Exception:
            return source_name, base_weight, []

    with ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        futures = {ex.submit(run_source, fn, sn, bw): sn for fn, sn, bw in SOURCES}
        for future in as_completed(futures):
            source_name, base_weight, candidates = future.result()
            for rank, (url, match_sc) in enumerate(candidates[:5]):
                domain = normalize_domain(url)
                if not domain:
                    continue
                sim        = domain_sim(company, domain)
                rank_bonus = max(0, 10 - rank * 3)
                weight     = base_weight + sim + rank_bonus + int(match_sc * 0.20)
                if domain in votes:
                    votes[domain][0] += weight + 25
                    votes[domain][2].append(source_name)
                else:
                    votes[domain] = [weight, url, [source_name]]
    return votes


def _merge(all_votes: dict, new_votes: dict):
    for domain, (score, url, sources) in new_votes.items():
        if domain in all_votes:
            all_votes[domain][0] += score + 25
            for s in sources:
                if s not in all_votes[domain][2]:
                    all_votes[domain][2].append(s)
        else:
            all_votes[domain] = [score, url, list(sources)]


def _best_validated(company: str, all_votes: dict, threshold: int):
    """Validate top candidates and return first that meets threshold, or None."""
    ranked = sorted(all_votes.items(), key=lambda x: -x[1][0])
    best   = {"url": None, "sources": [], "content": 0}

    for domain, (vote_score, url, source_list) in ranked:
        content_score = validate_url(company, url)
        n = len(source_list)
        if n >= 4:   content_score = min(100, content_score + 20)
        elif n >= 3: content_score = min(100, content_score + 12)
        elif n == 2: content_score = min(100, content_score + 6)

        if content_score > best["content"]:
            best = {"url": url, "sources": source_list, "content": content_score}
        if content_score >= 95:
            break

    if best["url"] and best["content"] >= threshold:
        c = best["content"]
        conf = "full" if c >= 95 else ("high" if c >= 80 else ("medium" if c >= 45 else "low"))
        return best["url"], " + ".join(best["sources"]), conf
    return None


def find_website(company: str, status_cb=None):
    def say(msg):
        if status_cb:
            status_cb(msg)

    all_votes: dict = {}

    # ── Round 1: standard search with original name ───────────────
    say(f"🔎 **{company}** — Round 1: searching all sources...")
    _merge(all_votes, _collect_votes(company, company))
    result = _best_validated(company, all_votes, threshold=45)
    if result:
        return result

    # ── Round 2: alternative name forms ──────────────────────────
    alt_names = list(dict.fromkeys(filter(None, [
        clean_name(company),
        clean_name(company).split()[0] if clean_name(company) else "",
        acronym(company),
    ])))
    for alt in alt_names:
        if not alt or alt.lower() == company.lower():
            continue
        say(f"🔄 **{company}** — Round 2: trying '{alt}'...")
        _merge(all_votes, _collect_votes(company, alt))
        result = _best_validated(company, all_votes, threshold=40)
        if result:
            return result

    # ── Round 3: broad search, no quotes, looser queries ─────────
    say(f"🔄 **{company}** — Round 3: broad search...")
    broad_votes: dict = {}
    for q in [f"{company} official website", f"{clean_name(company)} website", f"{company} company site"]:
        try:
            r = requests.get(f"https://www.bing.com/search?q={quote_plus(q)}",
                             headers=HEADERS, timeout=10)
            for url, sc in _parse_bing(r.text, company, limit=10):
                domain = normalize_domain(url)
                if not domain: continue
                w = 20 + domain_sim(company, domain) + int(sc * 0.15)
                if domain in broad_votes:
                    broad_votes[domain][0] += w
                else:
                    broad_votes[domain] = [w, url, ["Bing-broad"]]
        except Exception:
            pass
        try:
            for r in DDGS().text(q, max_results=10) or []:
                url = r.get("href", "")
                if not url or is_blocked(url): continue
                sc  = max(name_score(company, r.get("title", "")),
                          name_score(company, r.get("body",  "")))
                if sc < 35: continue
                domain = normalize_domain(url)
                if not domain: continue
                w = 20 + domain_sim(company, domain) + int(sc * 0.15)
                if domain in broad_votes:
                    broad_votes[domain][0] += w
                else:
                    broad_votes[domain] = [w, get_root_url(url), ["DDG-broad"]]
        except Exception:
            pass

    _merge(all_votes, broad_votes)
    result = _best_validated(company, all_votes, threshold=30)
    if result:
        url, src, _ = result
        return url, src, "low"

    # ── Absolute fallback: return highest-voted domain ────────────
    say(f"⚠️ **{company}** — returning best available result...")
    if all_votes:
        domain, (score, url, source_list) = sorted(all_votes.items(), key=lambda x: -x[1][0])[0]
        return url, " + ".join(source_list), "low"

    return "Not found", "—", "low"


# ── Sidebar ───────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Settings")
    delay       = st.number_input("Delay between companies (sec)", min_value=0.5, max_value=5.0, value=1.0, step=0.5)
    show_source = st.checkbox("Show source column", value=True)
    show_conf   = st.checkbox("Show confidence column", value=True)

# ── Input ─────────────────────────────────────────────────────────
company_input = st.text_area(
    "Company Names (one per line)",
    placeholder="Apple Inc\nGeneral Electric\nH&M\n...",
    height=200,
)

if st.button("🚀 Find Websites", use_container_width=True, type="primary"):
    companies = [c.strip() for c in company_input.strip().splitlines() if c.strip()]
    if not companies:
        st.warning("Please enter at least one company name.")
    else:
        results           = []
        progress          = st.progress(0, text="Starting...")
        status            = st.empty()
        table_placeholder = st.empty()

        for i, company in enumerate(companies):
            status.markdown(f"🔎 Searching **{company}**...")
            website, source, confidence = find_website(company, status_cb=status.markdown)

            conf_label = {"full": "💯 Full", "high": "✅ High", "medium": "🟡 Medium", "low": "🔴 Low"}.get(confidence, "—")
            results.append({"Company": company, "Website": website, "Confidence": conf_label, "Source": source})

            progress.progress((i + 1) / len(companies), text=f"{i+1}/{len(companies)} done")

            fields = ["Company", "Website"]
            if show_conf:   fields.append("Confidence")
            if show_source: fields.append("Source")
            display = [{k: r[k] for k in fields} for r in results]
            col_cfg = {"Website": st.column_config.LinkColumn("Website", display_text="Open")}
            table_placeholder.dataframe(display, use_container_width=True, column_config=col_cfg)

            if i < len(companies) - 1:
                time.sleep(delay)

        status.success(f"✅ Done! Searched {len(companies)} companies.")
        progress.empty()

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["Company", "Website", "Confidence", "Source"])
        writer.writeheader()
        writer.writerows(results)
        st.download_button("⬇️ Download CSV", buf.getvalue(),
                           "company_websites.csv", "text/csv", use_container_width=True)
