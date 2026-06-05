import time
import csv
import io
import re
import requests
import streamlit as st
from ddgs import DDGS
from urllib.parse import urlparse
from rapidfuzz import fuzz

st.set_page_config(page_title="Company Website Finder", page_icon="🔍", layout="centered")
st.title("🔍 Company Website Finder")
st.markdown("Paste company names below (one per line), then click **Find Websites**.")

SKIP_DOMAINS = [
    "linkedin.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "reddit.com",
    "bloomberg.com", "reuters.com", "forbes.com", "cnbc.com",
    "wikipedia.org", "crunchbase.com", "glassdoor.com",
    "indeed.com", "yelp.com", "zoominfo.com", "dnb.com",
    "owler.com", "pitchbook.com", "manta.com", "bbb.org",
    "trustpilot.com", "prnewswire.com", "businesswire.com",
    "tripadvisor.com", "booking.com", "expedia.com",
    "play.google.com",
]
SUFFIXES = {"inc", "ltd", "llc", "corp", "co", "plc", "gmbh", "ag",
            "sa", "bv", "nv", "ab", "oy", "as", "limited", "company"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

EXTRA_TLDS = ["com", "net", "org", "io", "co", "ai", "app"]


def clean_name(company):
    return " ".join(w for w in company.split() if w.lower().rstrip(".") not in SUFFIXES).strip()


def acronym(company):
    words = clean_name(company).split()
    return "".join(w[0] for w in words if w).lower() if len(words) > 1 else ""


def get_root_url(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.netloc else url


def normalize_domain(url):
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def is_blocked(url):
    return any(s in url for s in SKIP_DOMAINS)


def domain_similarity(company, domain):
    slug = re.sub(r"[^a-z0-9]", "", clean_name(company).lower())
    base = domain.split(".")[0].lower()
    return int(fuzz.ratio(slug, base) * 0.30)


# ── Content validation ────────────────────────────────────────────
def validate_url(company, url):
    try:
        r = requests.get(url, timeout=7, headers=HEADERS, allow_redirects=True)
        if r.status_code >= 400:
            return 0
        html = r.text[:10000]
        title_m = re.search(r"<title[^>]*>([^<]{1,200})</title>", html, re.IGNORECASE)
        meta_m  = re.search(r'<meta[^>]+name=["\'](?:description|application-name)["\'][^>]+content=["\']([^"\']{1,300})["\']', html, re.IGNORECASE)
        og_m    = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']{1,200})["\']', html, re.IGNORECASE)

        title = title_m.group(1).strip() if title_m else ""
        meta  = meta_m.group(1).strip()  if meta_m  else ""
        og    = og_m.group(1).strip()    if og_m    else ""

        query = clean_name(company)
        return max(
            fuzz.partial_ratio(query.lower(), title.lower()),
            fuzz.token_set_ratio(query.lower(), title.lower()),
            fuzz.partial_ratio(query.lower(), meta.lower()),
            fuzz.partial_ratio(query.lower(), og.lower()),
        )
    except Exception:
        return 0


# ── Source 1: Wikidata P856 ───────────────────────────────────────
def find_via_wikidata(company):
    try:
        search = requests.get("https://www.wikidata.org/w/api.php", headers=HEADERS, params={
            "action": "wbsearchentities", "search": company,
            "language": "en", "type": "item", "format": "json", "limit": 5
        }, timeout=10).json()
        for entity in search.get("search", []):
            label = entity.get("label", "")
            if fuzz.token_set_ratio(company.lower(), label.lower()) < 60:
                continue
            eid = entity["id"]
            claims = requests.get("https://www.wikidata.org/w/api.php", headers=HEADERS, params={
                "action": "wbgetentities", "ids": eid,
                "props": "claims", "format": "json"
            }, timeout=10).json().get("entities", {}).get(eid, {}).get("claims", {})
            if "P856" in claims:
                url = claims["P856"][0]["mainsnak"]["datavalue"]["value"]
                if url and not is_blocked(url):
                    return get_root_url(url)
    except Exception:
        pass
    return None


# ── Source 2: Wikipedia infobox ───────────────────────────────────
def find_via_wikipedia(company):
    try:
        search = requests.get("https://en.wikipedia.org/w/api.php", headers=HEADERS, params={
            "action": "query", "list": "search", "srsearch": company,
            "format": "json", "srlimit": 3
        }, timeout=8).json()
        for result in search.get("query", {}).get("search", []):
            title = result["title"]
            if fuzz.token_set_ratio(company.lower(), title.lower()) < 55:
                continue
            content = requests.get("https://en.wikipedia.org/w/api.php", headers=HEADERS, params={
                "action": "query", "titles": title, "prop": "revisions",
                "rvprop": "content", "rvslots": "main", "format": "json"
            }, timeout=8).json()
            for page in content.get("query", {}).get("pages", {}).values():
                text = page.get("revisions", [{}])[0].get("slots", {}).get("main", {}).get("*", "")
                match = re.search(r'\|\s*website\s*=\s*(?:\{\{URL\|)?([^\s|}\]\n]+)', text, re.IGNORECASE)
                if match:
                    url = match.group(1).strip().strip("{}")
                    if not url.startswith("http"):
                        url = "https://" + url
                    url = get_root_url(url)
                    if url and not is_blocked(url):
                        return url
    except Exception:
        pass
    return None


# ── Source 3: Clearbit autocomplete ──────────────────────────────
def find_via_clearbit(company, queries=None):
    if queries is None:
        queries = list(dict.fromkeys([clean_name(company), company]))
    first_word = clean_name(company).split()[0].lower() if clean_name(company) else ""
    for query in queries:
        try:
            results = requests.get(
                "https://autocomplete.clearbit.com/v1/companies/suggest",
                params={"query": query}, timeout=8
            ).json()
            candidates = []
            for r in results:
                name   = r.get("name", "")
                domain = r.get("domain", "")
                if not domain or is_blocked(domain):
                    continue
                dr  = domain.split(".")[0].lower()
                tld = domain.split(".")[-1].lower()
                score = max(
                    fuzz.token_set_ratio(company.lower(), name.lower()),
                    fuzz.token_sort_ratio(company.lower(), name.lower()),
                )
                if score < 55:
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


# ── Source 4: DuckDuckGo ─────────────────────────────────────────
def find_via_ddg(company, extra_queries=None):
    query = clean_name(company)
    search_queries = [
        f'"{company}" official website',
        f'{company} official site',
        f'{query} company homepage',
    ]
    if extra_queries:
        search_queries += extra_queries

    for attempt in range(3):
        try:
            for q in search_queries:
                for r in DDGS().text(q, max_results=10) or []:
                    url   = r.get("href", "")
                    title = r.get("title", "")
                    body  = r.get("body", "")
                    if not url or is_blocked(url):
                        continue
                    score = max(
                        fuzz.partial_ratio(query.lower(), title.lower()),
                        fuzz.partial_ratio(query.lower(), body.lower()),
                    )
                    if score >= 60:
                        return get_root_url(url)
            return None
        except Exception:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    return None


# ── Source 5: Domain guessing ─────────────────────────────────────
def find_via_domain_guess(company, extended=False):
    query     = clean_name(company)
    slug      = re.sub(r"[^a-z0-9]", "", query.lower().replace(" ", ""))
    slug_dash = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]", "-", query.lower())).strip("-")
    acr       = acronym(company)
    first     = query.split()[0].lower() if query else ""

    candidates = [f"https://{slug}.com"]
    if slug_dash != slug:
        candidates.append(f"https://{slug_dash}.com")

    if extended:
        # extra TLDs
        for tld in EXTRA_TLDS[1:]:
            candidates.append(f"https://{slug}.{tld}")
        # first word only
        if first and first != slug:
            candidates.append(f"https://{first}.com")
        # acronym
        if acr and len(acr) >= 2:
            candidates.append(f"https://{acr}.com")

    for url in candidates:
        if not slug:
            continue
        try:
            r = requests.head(url, timeout=5, allow_redirects=True, headers=HEADERS)
            if r.status_code < 400:
                return url
        except Exception:
            pass
    return None


SOURCE_WEIGHT = {
    "Wikidata":     50,
    "Wikipedia":    45,
    "Clearbit":     35,
    "DuckDuckGo":   25,
    "Domain guess": 15,
}


def collect_all_votes(company):
    """Always run ALL sources (standard + extended) for every company."""
    acr   = acronym(company)
    first = clean_name(company).split()[0] if clean_name(company) else ""

    clearbit_queries = list(dict.fromkeys([
        clean_name(company), company, first, acr
    ]))
    ddg_extra = [
        f"{company} website",
        f"{company} home",
        f'"{clean_name(company)}" .com',
        f"{acr} company official site" if acr else "",
    ]
    ddg_extra = [q for q in ddg_extra if q]

    sources = [
        (lambda c: find_via_wikidata(c),                         "Wikidata"),
        (lambda c: find_via_wikipedia(c),                        "Wikipedia"),
        (lambda c: find_via_clearbit(c, clearbit_queries),       "Clearbit"),
        (lambda c: find_via_ddg(c, ddg_extra),                   "DuckDuckGo"),
        (lambda c: find_via_domain_guess(c, extended=True),      "Domain guess"),
    ]

    votes: dict[str, list] = {}
    for fn, source_name in sources:
        result = fn(company)
        if not result:
            continue
        domain = normalize_domain(result)
        if not domain:
            continue
        sim    = domain_similarity(company, domain)
        weight = SOURCE_WEIGHT[source_name] + sim
        if domain in votes:
            votes[domain][0] += weight + 20   # cross-source agreement bonus
            votes[domain][2].append(source_name)
        else:
            votes[domain] = [weight, result, [source_name]]

    return votes


def find_website(company):
    # Always run all 5 sources for every company
    votes = collect_all_votes(company)

    if not votes:
        return "Not found", "—", "low"

    # Validate every unique candidate, keep best content score
    ranked = sorted(votes.items(), key=lambda x: -x[1][0])
    best = {"url": None, "sources": [], "content": 0}

    for domain, (vote_score, url, source_list) in ranked:
        content_score = validate_url(company, url)
        if content_score > best["content"]:
            best = {"url": url, "sources": source_list, "content": content_score}
        if content_score >= 80:
            break   # high confidence found — no need to check further

    if best["url"]:
        c = best["content"]
        confidence = "high" if c >= 80 else ("medium" if c >= 45 else "low")
        return best["url"], " + ".join(best["sources"]), confidence

    # If content validation returned 0 for all (blocked JS sites etc.), return top vote winner
    domain, (score, url, source_list) = ranked[0]
    return url, " + ".join(source_list), "low"


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
            website, source, confidence = find_website(company)

            conf_label = {"high": "✅ High", "medium": "🟡 Medium", "low": "🔴 Low"}.get(confidence, "—")
            results.append({
                "Company":    company,
                "Website":    website,
                "Confidence": conf_label,
                "Source":     source,
            })

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
