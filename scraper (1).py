#!/usr/bin/env python3
"""
Seed Price Scraper
===================
Scrapes product data (seed name, variety, category, pack size, price,
product page URL) from:

  - https://syngentavegetables.pk/products/
  - https://psc.gop.pk/shop/

Both sites run WooCommerce, so listing pages use the same markup
(`ul.products > li.product`). The script:

  1. Crawls every paginated listing page for each site.
  2. Pulls the quick facts available on the listing (title, price,
     product URL, WooCommerce category class).
  3. Visits each individual product page to pick up details that are
     only shown there (variety, pack size, Certified/Basic category,
     and per-pack-size prices for variable products).
  4. Merges the result into a CSV file, keeping price history:
       - If a row (same product + pack size) already exists and the
         price hasn't changed, only `last_checked` is updated.
       - If the price changed, `previous_price`, `price_changed_on`
         and `price` are updated.
       - New products are appended.
  5. Is meant to be re-run every 10 days by an external scheduler
     (GitHub Actions workflow) that invokes this script.

Usage
-----
    python scraper.py                 # scrape immediately, then exit
    python scraper.py --csv myfile.csv

Note: this script runs a single scrape and exits. Scheduling (e.g. every
10 days) is handled externally by GitHub Actions.

Requirements
------------
    pip install requests beautifulsoup4
"""

import argparse
import csv
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

SITES = [
    {
        "name": "Syngenta Vegetables Pakistan",
        "base_url": "https://syngentavegetables.pk",
        "listing_url": "https://syngentavegetables.pk/products/",
        # WooCommerce query var this theme uses for pagination
        "page_param": "product-page",
    },
    {
        "name": "Punjab Seed Corporation",
        "base_url": "https://psc.gop.pk",
        "listing_url": "https://psc.gop.pk/shop/",
        "page_param": "product-page",
    },
]

CSV_FILE = "seed_prices.csv"
CSV_FIELDS = [
    "site",
    "seed_name",
    "variety",
    "category",
    "pack_size",
    "price",
    "currency",
    "previous_price",
    "price_changed_on",
    "product_url",
    "first_seen",
    "last_checked",
]

REQUEST_TIMEOUT = 20
REQUEST_DELAY_RANGE = (1.5, 3.5)  # be polite between requests
RUN_EVERY_DAYS = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 SeedPriceTracker/1.0"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("seed_scraper")


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Product:
    site: str
    seed_name: str
    variety: str
    category: str
    pack_size: str
    price: Optional[float]
    currency: str
    product_url: str

    def key(self):
        # Uniquely identifies a row: same product page + pack size
        return (self.product_url, self.pack_size)


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

session = requests.Session()
session.headers.update(HEADERS)


def get_soup(url: str) -> Optional[BeautifulSoup]:
    """Fetch a URL and return a BeautifulSoup object, or None on failure."""
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as exc:
            wait = 2 ** attempt
            log.warning("Request failed (%s) for %s — retry in %ss", exc, url, wait)
            time.sleep(wait)
    log.error("Giving up on %s after 3 attempts", url)
    return None


def polite_sleep():
    time.sleep(random.uniform(*REQUEST_DELAY_RANGE))


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def parse_price(text: str) -> Optional[float]:
    """'Rs 3,569' / '₨ 3,569' -> 3569.0"""
    if not text:
        return None
    cleaned = re.sub(r"[^\d.]", "", text)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_category_from_classes(li_classes) -> str:
    """product_cat-tomato- -> 'Tomato' """
    for cls in li_classes:
        if cls.startswith("product_cat-"):
            raw = cls[len("product_cat-"):].strip("-")
            if raw and raw != "uncategorized":
                return raw.replace("-", " ").title()
    return ""


def guess_certified_or_basic(text: str) -> str:
    """Look for Certified / Basic in a slug, title or attribute text."""
    t = text.lower()
    if "certified" in t:
        return "Certified"
    if "basic" in t:
        return "Basic"
    return ""


PACK_SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(kg|g|gram|grams|gm|ml|litre|liter|l\b|seeds?|packet|pkt)",
    re.IGNORECASE,
)


def guess_pack_size(*texts) -> str:
    for text in texts:
        if not text:
            continue
        m = PACK_SIZE_RE.search(text)
        if m:
            return f"{m.group(1)}{m.group(2)}".replace(" ", "")
    return ""


# --------------------------------------------------------------------------
# Listing page scraping
# --------------------------------------------------------------------------

def iter_listing_pages(site: dict):
    """Yield BeautifulSoup for each paginated listing page of a site."""
    base_listing = site["listing_url"]
    page = 1
    seen_urls = set()

    while True:
        if page == 1:
            url = base_listing
        else:
            sep = "&" if "?" in base_listing else "?"
            url = f"{base_listing}{sep}{site['page_param']}={page}"

        if url in seen_urls:
            break
        seen_urls.add(url)

        log.info("Fetching listing page %s (page %d)", url, page)
        soup = get_soup(url)
        if soup is None:
            break

        products = soup.select("ul.products li.product")
        if not products:
            break

        yield soup

        # Determine if there's a next page
        next_link = soup.select_one("nav.woocommerce-pagination a.next")
        if not next_link:
            break

        page += 1
        polite_sleep()


def parse_listing_products(soup: BeautifulSoup, site: dict) -> list:
    """Extract the quick-look product data from one listing page."""
    results = []
    for li in soup.select("ul.products li.product"):
        link_tag = li.select_one("a.woocommerce-loop-product__link")
        title_tag = li.select_one("h2.woocommerce-loop-product__title, .woocommerce-loop-product__title")
        if not link_tag or not title_tag:
            continue

        product_url = urljoin(site["base_url"], link_tag.get("href", ""))
        title = title_tag.get_text(strip=True)

        price_span = li.select_one("span.price")
        price_val = None
        if price_span:
            ins_amount = price_span.select_one("ins .amount, ins .woocommerce-Price-amount")
            if ins_amount:
                price_val = parse_price(ins_amount.get_text())
            else:
                amount = price_span.select_one(".amount, .woocommerce-Price-amount")
                if amount:
                    price_val = parse_price(amount.get_text())

        li_classes = li.get("class", [])
        category_from_class = extract_category_from_classes(li_classes)

        results.append(
            {
                "title": title,
                "product_url": product_url,
                "listing_price": price_val,
                "category_from_class": category_from_class,
            }
        )
    return results


# --------------------------------------------------------------------------
# Product detail page scraping
# --------------------------------------------------------------------------

def parse_attributes_table(soup: BeautifulSoup) -> dict:
    """WooCommerce 'Additional information' tab: table.woocommerce-product-attributes"""
    attrs = {}
    table = soup.select_one("table.woocommerce-product-attributes")
    if not table:
        return attrs
    for row in table.select("tr"):
        label = row.select_one("th")
        value = row.select_one("td")
        if label and value:
            key = label.get_text(strip=True)
            val = value.get_text(" ", strip=True)
            attrs[key] = val
    return attrs


def parse_variations(soup: BeautifulSoup) -> list:
    """
    Variable WooCommerce products store all variation combinations
    (attributes + price) as JSON in data-product_variations on
    form.variations_form. Returns a list of dicts, or [] if the
    product is a simple product (single price).
    """
    form = soup.select_one("form.variations_form")
    if not form:
        return []
    raw = form.get("data-product_variations")
    if not raw or raw == "false":
        return []
    try:
        variations = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []

    out = []
    for v in variations:
        attrs = v.get("attributes", {})
        # attribute keys look like "attribute_pa_pack-size": "500g"
        attr_text = ", ".join(str(val) for val in attrs.values() if val)
        price = v.get("display_price") or v.get("price")
        out.append({"attributes": attr_text, "price": price})
    return out


def scrape_product_detail(product_url: str) -> dict:
    """
    Visit a single product page and pull out variety, pack size,
    category (Certified/Basic) and, for variable products, a list of
    (pack_size, price) variations.
    """
    detail = {
        "variety": "",
        "category": "",
        "pack_size": "",
        "variations": [],  # list of {"pack_size":..., "price":...}
        "single_price": None,
    }

    soup = get_soup(product_url)
    if soup is None:
        return detail

    # Product title on the detail page (often the variety / seed code)
    title_tag = soup.select_one("h1.product_title, h1.entry-title")
    title_text = title_tag.get_text(strip=True) if title_tag else ""

    short_desc = soup.select_one(".woocommerce-product-details__short-description")
    short_desc_text = short_desc.get_text(" ", strip=True) if short_desc else ""

    breadcrumb = soup.select_one("nav.woocommerce-breadcrumb")
    breadcrumb_text = breadcrumb.get_text(" ", strip=True) if breadcrumb else ""

    attrs = parse_attributes_table(soup)

    # --- Variety ---
    variety = (
        attrs.get("Variety")
        or attrs.get("Variety Name")
        or attrs.get("Hybrid")
        or title_text
    )
    detail["variety"] = variety

    # --- Category: Certified / Basic ---
    category = (
        attrs.get("Category")
        or attrs.get("Seed Category")
        or guess_certified_or_basic(product_url)
        or guess_certified_or_basic(title_text)
        or guess_certified_or_basic(breadcrumb_text)
        or guess_certified_or_basic(short_desc_text)
    )
    detail["category"] = category

    # --- Pack size ---
    pack_size = (
        attrs.get("Pack Size")
        or attrs.get("Packing")
        or attrs.get("Weight")
        or guess_pack_size(title_text, short_desc_text)
    )
    detail["pack_size"] = pack_size

    # --- Variations (variable products with multiple pack sizes) ---
    variations = parse_variations(soup)
    for v in variations:
        detail["variations"].append(
            {
                "pack_size": v["attributes"] or pack_size,
                "price": parse_price(str(v["price"])) if v["price"] is not None else None,
            }
        )

    # --- Simple product single price (fallback / cross-check) ---
    price_tag = soup.select_one("p.price, span.price")
    if price_tag:
        ins_amount = price_tag.select_one("ins .amount, ins .woocommerce-Price-amount")
        amount = ins_amount or price_tag.select_one(".amount, .woocommerce-Price-amount")
        if amount:
            detail["single_price"] = parse_price(amount.get_text())

    return detail


# --------------------------------------------------------------------------
# Site orchestration
# --------------------------------------------------------------------------

def scrape_site(site: dict) -> list:
    """Return a list of Product objects for one site."""
    products = []
    listing_items = []

    for soup in iter_listing_pages(site):
        listing_items.extend(parse_listing_products(soup, site))

    log.info("%s: found %d products on listing pages", site["name"], len(listing_items))

    for item in listing_items:
        polite_sleep()
        log.info("Visiting product page: %s", item["product_url"])
        detail = scrape_product_detail(item["product_url"])

        seed_name = item["category_from_class"] or detail["variety"] or item["title"]
        variety = detail["variety"] or item["title"]
        category = detail["category"]
        currency = "PKR"

        if detail["variations"]:
            # Variable product -> one CSV row per pack size
            for v in detail["variations"]:
                price = v["price"] if v["price"] is not None else item["listing_price"]
                products.append(
                    Product(
                        site=site["name"],
                        seed_name=seed_name,
                        variety=variety,
                        category=category,
                        pack_size=v["pack_size"] or "N/A",
                        price=price,
                        currency=currency,
                        product_url=item["product_url"],
                    )
                )
        else:
            price = detail["single_price"] if detail["single_price"] is not None else item["listing_price"]
            products.append(
                Product(
                    site=site["name"],
                    seed_name=seed_name,
                    variety=variety,
                    category=category,
                    pack_size=detail["pack_size"] or "N/A",
                    price=price,
                    currency=currency,
                    product_url=item["product_url"],
                )
            )

    return products


def scrape_all_sites() -> list:
    all_products = []
    for site in SITES:
        try:
            all_products.extend(scrape_site(site))
        except Exception:
            log.exception("Failed to scrape site: %s", site["name"])
    return all_products


# --------------------------------------------------------------------------
# CSV read / merge / write
# --------------------------------------------------------------------------

def load_existing_csv(path: str) -> dict:
    """Return {(product_url, pack_size): row_dict} from an existing CSV."""
    existing = {}
    if not os.path.exists(path):
        return existing
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            existing[(row["product_url"], row["pack_size"])] = row
    return existing


def merge_and_write_csv(path: str, scraped: list):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.now().strftime("%Y-%m-%d")

    existing = load_existing_csv(path)
    merged = {}
    updated_count = 0
    new_count = 0
    unchanged_count = 0

    for p in scraped:
        key = p.key()
        old_row = existing.get(key)

        if old_row is None:
            merged[key] = {
                "site": p.site,
                "seed_name": p.seed_name,
                "variety": p.variety,
                "category": p.category,
                "pack_size": p.pack_size,
                "price": p.price if p.price is not None else "",
                "currency": p.currency,
                "previous_price": "",
                "price_changed_on": today,
                "product_url": p.product_url,
                "first_seen": today,
                "last_checked": now,
            }
            new_count += 1
            continue

        old_price = old_row.get("price", "")
        new_price = p.price if p.price is not None else ""

        row = dict(old_row)
        row["site"] = p.site
        row["seed_name"] = p.seed_name or old_row.get("seed_name", "")
        row["variety"] = p.variety or old_row.get("variety", "")
        row["category"] = p.category or old_row.get("category", "")
        row["pack_size"] = p.pack_size or old_row.get("pack_size", "")
        row["last_checked"] = now

        price_changed = str(old_price).strip() != str(new_price).strip() and new_price != ""
        if price_changed:
            row["previous_price"] = old_price
            row["price"] = new_price
            row["price_changed_on"] = today
            updated_count += 1
            log.info(
                "PRICE CHANGE: %s (%s) — %s -> %s",
                row["variety"], row["pack_size"], old_price, new_price,
            )
        else:
            unchanged_count += 1

        merged[key] = row

    # Keep any old rows that weren't seen this run (product may be
    # temporarily out of stock / removed from listing) instead of
    # silently deleting price history.
    for key, old_row in existing.items():
        if key not in merged:
            merged[key] = old_row

    folder = os.path.dirname(path)
    if folder and not os.path.exists(folder):
        os.makedirs(folder, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in merged.values():
            writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})

    log.info(
        "CSV updated: %d new, %d price changes, %d unchanged (total rows: %d) -> %s",
        new_count, updated_count, unchanged_count, len(merged), path,
    )


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def run_once(csv_path: str):
    log.info("=== Starting scrape run ===")
    scraped = scrape_all_sites()
    if not scraped:
        log.warning("No products scraped this run — CSV left untouched.")
        return
    merge_and_write_csv(csv_path, scraped)
    log.info("=== Scrape run finished ===")


def main():
    parser = argparse.ArgumentParser(description="Seed price scraper")
    parser.add_argument(
        "--csv",
        default=CSV_FILE,
        help="Path to the output CSV file",
    )
    args = parser.parse_args()
    run_once(args.csv)


if __name__ == "__main__":
    main()
