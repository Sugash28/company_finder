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

# ── Source 1: Wikidata (official website property P856) ──────────
HEADERS = {"User-Agent": "CompanyWebsiteFinder/1.0 (research tool)"}

def find_via_wikidata(company):
    try:
        search = requests.get("https://www.wikidata.org/w/api.php", headers=HEADERS, params={
            "action": "wbsearchentities", "search": company,
            "language": "en", "type": "item", "format": "json", "limit": 5
        }, timeout=10).json()

        for entity in search.get("search", []):
            label = entity.get("label", "")
            if fuzz.token_set_ratio(company.lower(), label.lower()) < 70:
                continue
            eid = entity["id"]
            claims = requests.get("https://www.wikidata.org/w/api.php", headers=HEADERS, params={
                "action": "wbgetentities", "ids": eid,
                "props": "claims", "format": "json"
            }, timeout=10).json().get("entities", {}).get(eid, {}).get("claims", {})

            if "P856" in claims:  # P856 = official website
                url = claims["P856"][0]["mainsnak"]["datavalue"]["value"]
                if url and not is_blocked(url):
                    return url
    except Exception:
        pass
    return None

# ── Source 2: Clearbit ───────────────────────────────────────────
def find_via_clearbit(company):
    query      = clean_name(company)
    first_word = query.split()[0].lower()
    q_clean    = re.sub(r"[^a-z0-9 ]", "", query.lower())
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

            name_score = fuzz.token_set_ratio(query.lower(), name.lower())
            # Only accept if name closely matches
            if name_score < 75:
                continue

            bonus = (15 if dr.startswith(first_word[:4]) else 0) + \
                    (10 if tld in ("com", "net", "org") else 0)
            candidates.append((name_score + bonus, len(domain), domain, name))

        candidates.sort(key=lambda x: (-x[0], x[1]))
        if candidates:
            return f"https://{candidates[0][2]}"
    except Exception:
        pass
    return None

# ── Source 3: DuckDuckGo search ──────────────────────────────────
def find_via_ddg(company):
    query = clean_name(company)
    try:
        for q in [f'"{company}" official website', f'{company} homepage']:
            ddg = DDGS().text(q, max_results=15)
            for r in ddg or []:
                url   = r.get("href", "")
                title = r.get("title", "")
                if not url or is_blocked(url):
                    continue
                if fuzz.partial_ratio(query.lower(), title.lower()) >= 80:
                    return get_root_url(url)
    except Exception:
        pass
    return None

def find_website(company):
    return (
        find_via_wikidata(company) or
        find_via_clearbit(company) or
        find_via_ddg(company) or
        "Not found"
    )

# ── Sidebar ──────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Settings")
    delay = st.number_input("Delay (sec)", min_value=0.5, max_value=5.0, value=1.0, step=0.5)

# ── Input ────────────────────────────────────────────────────────
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
            website = find_website(company)
            results.append({"Company": company, "Website": website})
            progress.progress((i + 1) / len(companies), text=f"{i+1}/{len(companies)} done")
            table_placeholder.dataframe(
                results, use_container_width=True,
                column_config={"Website": st.column_config.LinkColumn("Website", display_text="Open")}
            )
            if i < len(companies) - 1:
                time.sleep(delay)

        status.success(f"✅ Done! Searched {len(companies)} companies.")
        progress.empty()

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["Company", "Website"])
        writer.writeheader()
        writer.writerows(results)
        st.download_button("⬇️ Download CSV", buf.getvalue(),
                           "company_websites.csv", "text/csv", use_container_width=True)
