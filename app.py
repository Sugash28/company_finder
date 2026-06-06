import ssl
import socket
import json
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


# ── SSL certificate org check ─────────────────────────────────────
def ssl_org_score(company, domain):
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
                    query = clean_name(company)
                    return max(
                        fuzz.token_set_ratio(query.lower(), val.lower()),
                        fuzz.partial_ratio(query.lower(), val.lower()),
                    )
    except Exception:
        pass
    return 0


# ── Content validation ────────────────────────────────────────────
def validate_url(company, url):
    try:
        r = requests.get(url, timeout=7, headers=HEADERS, allow_redirects=True)
        if r.status_code >= 400:
            return 0
        html  = r.text[:20000]
        query = clean_name(company)

        # 1. JSON-LD structured data
        for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE):
            try:
                data  = json.loads(block.strip())
                items = data if isinstance(data, list) else [data]
                for item in items:
                    name = item.get("name", "") or item.get("legalName", "")
                    if not name:
                        continue
                    score = max(
                        fuzz.token_set_ratio(query.lower(), name.lower()),
                        fuzz.partial_ratio(query.lower(), name.lower()),
                    )
                    if score >= 75:
                        return 98
            except Exception:
                pass

        # 2. SSL certificate
        domain     = normalize_domain(url)
        cert_score = ssl_org_score(company, domain)
        if cert_score >= 75:
            return 95

        # 3. Title / og:site_name / meta / copyright / h1
        title_m = re.search(r"<title[^>]*>([^<]{1,200})</title>", html, re.IGNORECASE)
        meta_m  = re.search(r'<meta[^>]+name=["\'](?:description|application-name)["\'][^>]+content=["\']([^"\']{1,300})["\']', html, re.IGNORECASE)
        og_m    = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']{1,200})["\']', html, re.IGNORECASE)
        copy_m  = re.search(r'©\s*(?:\d{4}[–\-]?\d{0,4})?\s*([^<\n]{2,80})', html)
        h1_m    = re.search(r'<h1[^>]*>([^<]{1,150})</h1>', html, re.IGNORECASE)

        title = title_m.group(1).strip() if title_m else ""
        meta  = meta_m.group(1).strip()  if meta_m  else ""
        og    = og_m.group(1).strip()    if og_m    else ""
        copy  = copy_m.group(1).strip()  if copy_m  else ""
        h1    = h1_m.group(1).strip()    if h1_m    else ""

        return max(
            fuzz.partial_ratio(query.lower(), title.lower()),
            fuzz.token_set_ratio(query.lower(), title.lower()),
            fuzz.partial_ratio(query.lower(), og.lower()),
            fuzz.partial_ratio(query.lower(), meta.lower()),
            fuzz.partial_ratio(query.lower(), copy.lower()),
            fuzz.partial_ratio(query.lower(), h1.lower()),
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


# ── Source 3: DDG Instant Answers ────────────────────────────────
def find_via_ddg_instant(company):
    query = clean_name(company)
    try:
        data = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": company, "format": "json", "no_redirect": "1", "no_html": "1"},
            headers=HEADERS, timeout=8
        ).json()

        for item in data.get("Infobox", {}).get("content", []):
            if item.get("label", "").lower() in ("official website", "website"):
                url = item.get("value", "")
                if url and not is_blocked(url):
                    return get_root_url(url)

        name = data.get("Heading", "")
        abstract_url = data.get("AbstractURL", "")
        if abstract_url and not is_blocked(abstract_url):
            if fuzz.token_set_ratio(query.lower(), name.lower()) >= 55:
                return get_root_url(abstract_url)

        for rel in data.get("RelatedTopics", []):
            url  = rel.get("FirstURL", "")
            text = rel.get("Text", "")
            if url and not is_blocked(url) and fuzz.partial_ratio(query.lower(), text.lower()) >= 70:
                return get_root_url(url)
    except Exception:
        pass
    return None


# ── Source 4: Clearbit autocomplete ──────────────────────────────
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


# ── Source 5: DuckDuckGo search ──────────────────────────────────
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


# ── Source 6: Domain guessing ─────────────────────────────────────
def find_via_domain_guess(company):
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

    for url in candidates:
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
    "DDG Instant":  40,
    "Clearbit":     35,
    "DuckDuckGo":   25,
    "Domain guess": 15,
}


def find_website(company):
    acr   = acronym(company)
    first = clean_name(company).split()[0] if clean_name(company) else ""

    clearbit_queries = list(dict.fromkeys([clean_name(company), company, first, acr]))
    ddg_extra = [q for q in [
        f"{company} website",
        f"{company} home",
        f'"{clean_name(company)}" .com',
        f"{acr} company official site" if acr else "",
    ] if q]

    sources = [
        (lambda c: find_via_wikidata(c),                   "Wikidata"),
        (lambda c: find_via_wikipedia(c),                  "Wikipedia"),
        (lambda c: find_via_ddg_instant(c),                "DDG Instant"),
        (lambda c: find_via_clearbit(c, clearbit_queries), "Clearbit"),
        (lambda c: find_via_ddg(c, ddg_extra),             "DuckDuckGo"),
        (lambda c: find_via_domain_guess(c),               "Domain guess"),
    ]

    votes: dict = {}
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
            votes[domain][0] += weight + 20
            votes[domain][2].append(source_name)
        else:
            votes[domain] = [weight, result, [source_name]]

    if not votes:
        return "Not found", "—", "low"

    ranked = sorted(votes.items(), key=lambda x: -x[1][0])
    best   = {"url": None, "sources": [], "content": 0}

    for domain, (vote_score, url, source_list) in ranked:
        content_score = validate_url(company, url)
        if len(source_list) >= 3:
            content_score = min(100, content_score + 15)
        elif len(source_list) == 2:
            content_score = min(100, content_score + 8)

        if content_score > best["content"]:
            best = {"url": url, "sources": source_list, "content": content_score}

        if content_score >= 95:
            break

    if best["url"]:
        c = best["content"]
        confidence = "full" if c >= 95 else ("high" if c >= 80 else ("medium" if c >= 45 else "low"))
        return best["url"], " + ".join(best["sources"]), confidence

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
