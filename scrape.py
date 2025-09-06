# x_scraper.py
import os
import time
import random
import csv
from urllib.parse import quote_plus

import pandas as pd
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager


# ------------------- YOUR QUERIES (same buckets) -------------------
NEGATIVE_QUERIES = [
    "#FreeKashmir", "StandWithKashmir", "Indian Occupied Kashmir",
    "India is fascist", "Modi is Hitler", "RSS terrorism",
    "India genocide", "Boycott India", "Hindutva terrorism", "Sanction India"
]

POSITIVE_QUERIES = [
    "Jai Hind", "#ProudToBeIndian", "India is great", "I love India",
    "Incredible India", "India rising", "Support India", "India my pride"
]

NEUTRAL_QUERIES = [
    "India news", "India economy", "India culture", "India cricket",
    "India tourism", "India technology", "India education"
]


# ------------------- DRIVER SETUP -------------------
def get_driver(headless=False):
    options = webdriver.ChromeOptions()
    # Comment headless if you want to see the browser; headless can be blocked more often.
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    # Improve stability
    prefs = {"profile.default_content_setting_values.notifications": 2}
    options.add_experimental_option("prefs", prefs)

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    driver.set_page_load_timeout(60)
    return driver


# ------------------- OPTIONAL: MANUAL LOGIN -------------------
def ensure_logged_in(driver, timeout=180):
    """
    Opens X login page. You log in manually once.
    We wait until the profile/menu appears or timeout passes.
    """
    # New domain is often x.com; twitter.com redirects.
    driver.get("https://x.com/login")
    # Give you time to type creds, solve 2FA, etc.
    print(f"Please complete login within ~{timeout} seconds…")
    end = time.time() + timeout

    # We consider login successful if we can see the top nav bar with 'Search' input or home timeline.
    while time.time() < end:
        time.sleep(3)
        cur = driver.current_url
        # If you land on /home or /search or anything besides /login, likely logged in.
        if "/login" not in cur:
            try:
                # Look for search box or side navigation
                driver.find_element(By.XPATH, "//input[@data-testid='SearchBox_Search_Input' or @placeholder='Search']")
                print("✅ Login detected (search input present).")
                return True
            except:
                # Try to detect profile link or Home nav
                try:
                    driver.find_element(By.XPATH, "//a[contains(@href, '/home') or @aria-label='Profile']")
                    print("✅ Login detected (home/profile present).")
                    return True
                except:
                    pass
    print("⚠️ Proceeding without confirming login; some results may be limited.")
    return False


# ------------------- CORE SCRAPING -------------------
def open_search_live(driver, query):
    """
    Opens the 'Latest' (live) tab for a given query.
    """
    q = quote_plus(query)
    # Use x.com which is current. Fallback to twitter.com if needed.
    url = f"https://x.com/search?q={q}&src=typed_query&f=live"
    driver.get(url)
    # Wait for results to load or tolerate fallback
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((
                By.XPATH, "//article[.//a[contains(@href,'/status/')]]"
            ))
        )
    except:
        time.sleep(5)  # fallback wait


def extract_tweets_from_page(driver):
    """
    Parse current DOM with BeautifulSoup and return a list of dictionaries.
    We try robust selectors; X changes often.
    """
    html = driver.page_source
    soup = BeautifulSoup(html, "lxml")

    # Strategy: find 'article' nodes that contain a status link
    # We can't use :has in BeautifulSoup, so we collect and filter.
    articles = soup.find_all("article")
    rows = []

    for art in articles:
        # Find a status link -> /<user>/status/<id>
        status_a = None
        for a in art.find_all("a", href=True):
            if "/status/" in a["href"]:
                status_a = a
                break
        if not status_a:
            continue

        tweet_url = status_a["href"]
        # Normalize to full URL
        if tweet_url.startswith("/"):
            full_url = "https://x.com" + tweet_url
        else:
            full_url = tweet_url

        # Extract tweet_id + username from URL
        try:
            parts = tweet_url.strip("/").split("/")
            # parts like: ['', '<user>', 'status', '<id>'] or ['<user>', 'status', '<id>']
            if "status" in parts:
                i = parts.index("status")
                username = parts[i - 1] if i - 1 >= 0 else ""
                tweet_id = parts[i + 1].split("?")[0] if i + 1 < len(parts) else ""
            else:
                username, tweet_id = "", ""
        except Exception:
            username, tweet_id = "", ""

        # Text container often lives in div[data-testid="tweetText"] with multiple spans
        text_container = art.find("div", attrs={"data-testid": "tweetText"})
        if text_container:
            text = " ".join(s.get_text(strip=True) for s in text_container.find_all(["span", "a"]))
        else:
            # Fallback: article text minus link crumbs; not perfect but robust
            text = art.get_text(" ", strip=True)

        # Timestamp (if present)
        t_tag = art.find("time")
        created_at = t_tag.get("datetime") if t_tag else ""

        rows.append({
            "tweet_id": tweet_id,
            "username": username,
            "created_at": created_at,
            "text": text,
            "url": full_url
        })

    return rows


def infinite_scroll_collect(driver, target_count=100, max_scrolls=200, pause=(1.5, 3.0)):
    """
    Scrolls the page, collects tweets, and stops when target_count or max_scrolls reached.
    Returns list of tweet dicts (unique by tweet_id or url).
    """
    seen = set()
    collected = []
    same_count_streak = 0

    for i in range(max_scrolls):
        # Extract current batch
        rows = extract_tweets_from_page(driver)
        new = 0
        for r in rows:
            key = r["tweet_id"] or r["url"]
            if key and key not in seen:
                seen.add(key)
                collected.append(r)
                new += 1

        if len(collected) >= target_count:
            break

        # If no new items for several scrolls, break (end of feed / throttled)
        if new == 0:
            same_count_streak += 1
        else:
            same_count_streak = 0

        if same_count_streak >= 5:
            # Likely at hard stop (rate-limited UI or no more results)
            break

        # Scroll a bit; using JS to bottom
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(random.uniform(*pause))

    return collected[:target_count]


# ------------------- CSV HELPERS -------------------
def append_to_csv(csv_path, rows, keyword, label):
    """
    Append rows to CSV with only two columns: content + label.
    """
    if not rows:
        return

    # Keep only tweet text + label
    df = pd.DataFrame([{"content": r["text"], "label": label} for r in rows])

    # Create file with header if missing; else append
    if not os.path.exists(csv_path):
        df.to_csv(csv_path, index=False, encoding="utf-8", quoting=csv.QUOTE_ALL)
    else:
        df.to_csv(csv_path, mode="a", header=False, index=False, encoding="utf-8", quoting=csv.QUOTE_ALL)


# ------------------- PUBLIC API (like your Tweepy function) -------------------
def scrape_queries(queries, label, per_query_limit=120, csv_path="twitte_csv"
".csv"):
    """
    For each query, open live search, scroll and collect tweets, then append to CSV.
    """
    driver = get_driver(headless=False)  # See the browser; easier for manual login
    try:
        # Strongly recommended: Login once so scrolling works well
        ensure_logged_in(driver, timeout=180)

        grand_total = 0
        for q in queries:
            print(f"\n🔎 Query: {q}  (Label={label})")
            open_search_live(driver, q)
            # Short warm-up pause
            time.sleep(random.uniform(2.0, 4.0))

            rows = infinite_scroll_collect(driver, target_count=per_query_limit, max_scrolls=250)
            append_to_csv(csv_path, rows, keyword=q, label=label)
            grand_total += len(rows)
            print(f"✅ Saved {len(rows)} tweets for '{q}' → {csv_path}")

            # polite pause between queries
            time.sleep(random.uniform(4.0, 8.0))

        print(f"\n🎉 Done. Collected {grand_total} tweets for label {label}.")
    finally:
        driver.quit()


if __name__ == "__main__":
    # Run in three buckets like your Tweepy code
    scrape_queries(NEGATIVE_QUERIES, "Negative", per_query_limit=120)
    scrape_queries(POSITIVE_QUERIES, "Positive", per_query_limit=120)
    scrape_queries(NEUTRAL_QUERIES, "Neutral",  per_query_limit=120)

    # Quick check
    if os.path.exists("india_sentiment_tweets_selenium.csv"):
        df = pd.read_csv("india_sentiment_tweets_selenium.csv")
        print("\nCounts by Label:")
        print(df["Label"].value_counts())
