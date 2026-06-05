import requests
import time

# ❗ DO NOT hardcode exposed keys in shared code
API_KEY = "AIzaSyBpHpDBxQVES2fe6dC8lZ2DQizyPzMNwmQ"
CX = "f6b6f6b65810741d7"


def get_official_website(company_name):
    url = "https://www.googleapis.com/customsearch/v1"

    params = {
        "q": f"{company_name} official website",
        "key": API_KEY,
        "cx": CX,
        "num": 5
    }

    response = requests.get(url, params=params)

    if response.status_code != 200:
        print("Error:", response.text)
        return None

    data = response.json()

    # Filter best result
    bad_sites = ["linkedin.com", "wikipedia.org", "crunchbase.com", "facebook.com"]

    for item in data.get("items", []):
        link = item.get("link", "")

        if not any(bad in link for bad in bad_sites):
            return link

    return None


def scrape_company_list(companies):
    results = {}

    for company in companies:
        print(f"Searching: {company}")
        results[company] = get_official_website(company)
        time.sleep(1)  # avoid quota/rate limit

    return results


# Example input list
companies = [
    "Tesla",
    "Infosys",
    "Apple",
    "Tata Motors",
    "Microsoft"
]

output = scrape_company_list(companies)

# Print results
for company, website in output.items():
    print(f"{company} -> {website}")