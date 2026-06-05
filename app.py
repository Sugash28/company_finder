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
    "apple.com/app-store", "play.google.com",
]
SUFFIXES = {"inc", "ltd", "llc", "corp", "co", "plc", "gmbh", "ag",
            "sa", "bv", "nv", "ab", "oy", "as", "limited", "company"}

HEADERS = {"User-Agent": "CompanyWebsiteFinder/1.0 (research tool)"}


def clean_name(company):
    return " ".join(w for w in company.split() if w.lower().rstrip(".") not in SUFFIXES).strip()


def get_root_url(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.netloc else url


def is_blocked(url):
    return any(s in url for s in SKIP_DOMAINS)


# ── Source 1: Wikidata (official website property P856) ──────────
def find_via_wikidata(company):
    try:
        search = requests.get("https://www.wikidata.org/w/api.php", headers=HEADERS, params={
            "action": "wbsearchentities", "search": company,
            "language": "en", "type": "item", "format": "json", "limit": 5
        }, timeout=10).json()

        for entity in search.get("search", []):
            label = entity.get("label", "")
            if fuzz.token_set_ratio(company.lower(), label.lower()) < 65:
                continue
            eid = entity["id"]
            claims = requests.get("https://www.wikidata.org/w/api.php", headers=HEADERS, params={
                "action": "wbgetentities", "ids": eid,
                "props": "claims", "format": "json"
            }, timeout=10).json().get("entities", {}).get(eid, {}).get("claims", {})

            if "P856" in claims:
                url = claims["P856"][0]["mainsnak"]["datavalue"]["value"]
                if url and not is_blocked(url):
                    return url
    except Exception:
        pass
    return None


# ── Source 2: Clearbit autocomplete ──────────────────────────────
def find_via_clearbit(company):
    queries = list(dict.fromkeys([clean_name(company), company]))  # deduplicated
    first_word = clean_name(company).split()[0].lower() if clean_name(company) else ""

    for query in queries:
        try:
            results = requests.get(
                "https://autocomplete.clearbit.com/v1/companies/suggest",
                params={"query": query}, timeout=8
            ).json()
            candidates = []
            for r in results:
                name = r.get("name", "")
                domain = r.get("domain", "")
                if not domain or is_blocked(domain):
                    continue
                dr = domain.split(".")[0].lower()
                tld = domain.split(".")[-1].lower()

                name_score = max(
                    fuzz.token_set_ratio(company.lower(), name.lower()),
                    fuzz.token_sort_ratio(company.lower(), name.lower()),
                )
                if name_score < 65:
                    continue

                bonus = (15 if first_word and dr.startswith(first_word[:4]) else 0) + \
                        (10 if tld in ("com", "net", "org") else 0)
                candidates.append((name_score + bonus, len(domain), domain, name))

            candidates.sort(key=lambda x: (-x[0], x[1]))
            if candidates:
                return f"https://{candidates[0][2]}"
        except Exception:
            pass
    return None


# ── Source 3: DuckDuckGo search (with retries) ───────────────────
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


# ── Source 4: Domain name guess ───────────────────────────────────
def find_via_domain_guess(company):
    query = clean_name(company)
    slug = re.sub(r"[^a-z0-9]", "", query.lower().replace(" ", ""))
    slug_dash = re.sub(r"[^a-z0-9-]", "-", query.lower()).strip("-")
    slug_dash = re.sub(r"-{2,}", "-", slug_dash)

    candidates = []
    if slug:
        candidates.append(f"https://{slug}.com")
    if slug_dash and slug_dash != slug:
        candidates.append(f"https://{slug_dash}.com")

    for url in candidates:
        try:
            r = requests.head(url, timeout=5, allow_redirects=True, headers=HEADERS)
            if r.status_code < 400:
                return url
        except Exception:
            pass
    return None


def find_website(company):
    for fn, source in [
        (find_via_wikidata, "Wikidata"),
        (find_via_clearbit, "Clearbit"),
        (find_via_ddg, "DuckDuckGo"),
        (find_via_domain_guess, "Domain guess"),
    ]:
        result = fn(company)
        if result:
            return result, source
    return "Not found", "—"


# ── Sidebar ───────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Settings")
    delay = st.number_input("Delay between requests (sec)", min_value=0.5, max_value=5.0, value=1.0, step=0.5)
    show_source = st.checkbox("Show source column", value=True)

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
        results = []
        progress = st.progress(0, text="Starting...")
        status = st.empty()
        table_placeholder = st.empty()

        for i, company in enumerate(companies):
            status.markdown(f"🔎 Searching **{company}**...")
            website, source = find_website(company)
            results.append({"Company": company, "Website": website, "Source": source})
            progress.progress((i + 1) / len(companies), text=f"{i+1}/{len(companies)} done")

            display = results if show_source else [{"Company": r["Company"], "Website": r["Website"]} for r in results]
            col_cfg = {"Website": st.column_config.LinkColumn("Website", display_text="Open")}
            table_placeholder.dataframe(display, use_container_width=True, column_config=col_cfg)

            if i < len(companies) - 1:
                time.sleep(delay)

        status.success(f"✅ Done! Searched {len(companies)} companies.")
        progress.empty()

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["Company", "Website", "Source"])
        writer.writeheader()
        writer.writerows(results)
        st.download_button("⬇️ Download CSV", buf.getvalue(),
                           "company_websites.csv", "text/csv", use_container_width=True)
