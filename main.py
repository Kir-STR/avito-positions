#!/usr/bin/env python3
"""Avito position tracker — monitors ad positions across cities."""

import argparse
import asyncio
import csv
import json
import logging
import random
import signal
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, unquote

from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
LOGS_DIR = BASE_DIR / "logs"

collected_results: list[dict] = []
shutdown_requested = False

logger = logging.getLogger("avito")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(path: Path | None = None) -> dict:
    path = path or BASE_DIR / "config.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}

    defaults = {
        "headless": True,
        "min_delay": 3,
        "max_delay": 12,
        "long_pause_every": 25,
        "long_pause_min": 15,
        "long_pause_max": 30,
        "page_timeout": 30000,
        "selector_timeout": 10000,
        "max_retries": 2,
    }
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    return cfg


def parse_category_path(url: str) -> tuple[str, str | None]:
    """Extract category_path and optional q= query from Avito URL.

    Returns (category_path, "q=<value>") for search URLs,
            (category_path, None) for plain category URLs.
    """
    parsed = urlparse(url)
    for prefix in ("https://www.avito.ru/all/", "http://www.avito.ru/all/"):
        if url.startswith(prefix):
            path = parsed.path[len("/all/"):]
            qs = parse_qs(parsed.query)
            if "q" in qs:
                return (path, urlencode({"q": qs["q"][0]}))
            return (path, None)
    raise ValueError(f"URL must start with https://www.avito.ru/all/  — got: {url}")


def load_cities(path: Path | None = None) -> list[str]:
    """Load city slugs from txt file (one per line, # comments allowed)."""
    path = path or BASE_DIR / "cities.txt"
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def load_keywords(path: Path | None = None) -> list[list[str]]:
    """Load keyword groups from txt file.

    Each line is one AND-group (all words on the line must match).
    Different lines are OR'd (any line matching is enough).
    """
    path = path or BASE_DIR / "keywords.txt"
    groups = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                groups.append(line.split())
    return groups


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def setup_logging():
    LOGS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    fh = logging.FileHandler(LOGS_DIR / f"run_{ts}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)

    logger.setLevel(logging.DEBUG)
    logger.addHandler(fh)
    logger.addHandler(ch)


def is_mine(title: str, keyword_groups: list[list[str]]) -> bool:
    """Check if ad title matches any keyword group (AND within group, OR between groups)."""
    title_lower = title.lower()
    return any(
        all(word.lower() in title_lower for word in group)
        for group in keyword_groups
    )


def save_results(results: list[dict], tag: str = ""):
    """Write results to CSV and JSON. Called incrementally and on shutdown."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    if not results:
        return

    ts = results[0].get("_run_ts", datetime.now().strftime("%Y%m%d_%H%M%S"))
    csv_path = OUTPUT_DIR / f"results_{ts}.csv"
    json_path = OUTPUT_DIR / f"results_{ts}.json"

    # CSV — rewrite entire file (list is small enough)
    fieldnames = [
        "city", "ad_position", "ad_title", "ad_url",
        "ad_is_reklama", "is_mine", "seller_name", "seller_url",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    # JSON
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)

    logger.debug("Saved %d records → %s / %s", len(results), csv_path.name, json_path.name)


def print_report(results: list[dict]):
    """Print a summary table grouped by city."""
    if not results:
        return
    # Group by city preserving order
    cities: dict[str, list[dict]] = {}
    for r in results:
        cities.setdefault(r["city"], []).append(r)

    # Column widths
    max_city = max(len(c) for c in cities)
    col_city = max(max_city, 5)  # "Город"

    header = (
        f"{'Город':<{col_city}} | Объявл. | Моих | Позиция"
    )
    sep = "-" * col_city + "-+--------+------+" + "-" * 20
    print(f"\n{header}\n{sep}")
    total_ads = total_mine = 0
    for city, ads in cities.items():
        n = len(ads)
        mine = [a for a in ads if a.get("is_mine")]
        m = len(mine)
        positions = ", ".join(str(a["ad_position"]) for a in mine) or "—"
        print(f"{city:<{col_city}} | {n:>6} | {m:>4} | {positions}")
        total_ads += n
        total_mine += m
    print(sep)
    print(f"{'ИТОГО':<{col_city}} | {total_ads:>6} | {total_mine:>4} |")
    print()


def save_debug_html(html: str, city: str):
    LOGS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOGS_DIR / f"debug_{city}_{ts}.html"
    path.write_text(html, encoding="utf-8")
    logger.warning("Debug HTML saved: %s", path.name)


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------
async def create_browser(pw, cfg: dict):
    browser = await pw.chromium.launch(
        headless=cfg["headless"],
        channel="chrome",
    )
    context = await browser.new_context(
        viewport={"width": 1366, "height": 768},
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    # Remove webdriver flag
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """)
    page = await context.new_page()
    return browser, context, page


async def warm_cookies(page, cfg: dict):
    """Visit homepage to get cookies before scraping."""
    logger.info("Warming cookies on avito.ru …")
    await page.goto("https://www.avito.ru", wait_until="domcontentloaded",
                    timeout=cfg["page_timeout"])
    await asyncio.sleep(random.uniform(3, 5))


async def smooth_scroll(page, times: int = 3):
    """Simulate human-like scrolling."""
    for _ in range(times):
        await page.evaluate("window.scrollBy(0, window.innerHeight * 0.7)")
        await asyncio.sleep(random.uniform(0.5, 1.5))


EXTRACT_ADS_JS = """
() => {
    const container = document.querySelector('[data-marker="catalog-serp"]');
    if (!container) return null;

    const items = container.querySelectorAll('[data-marker="item"]');
    const ads = [];

    items.forEach((item, index) => {
        // Title & ad link: <a data-marker="item-title">
        const titleLink = item.querySelector('a[data-marker="item-title"]');
        const adTitle = titleLink ? titleLink.textContent.trim() : '';
        const adUrl = titleLink ? titleLink.href.split('?')[0] : '';

        const isReklama = item.innerText.includes('Реклама');

        // Seller link: contains /brands/ or /user/ with ?src=search_seller_info
        let sellerName = '';
        let sellerUrl = '';
        const allLinks = item.querySelectorAll('a');
        for (const a of allLinks) {
            if (a.href && a.href.includes('src=search_seller_info')) {
                sellerName = a.textContent.trim().replace(/[\\d.,]+·.*$/, '').trim();
                sellerUrl = a.href.split('?')[0];
                break;
            }
        }

        ads.push({
            ad_position: index + 1,
            ad_title: adTitle,
            ad_url: adUrl,
            ad_is_reklama: isReklama,
            seller_name: sellerName,
            seller_url: sellerUrl,
        });
    });

    return ads;
}
"""


async def extract_ads(page, city: str, cfg: dict) -> list[dict] | None:
    """Extract ad listings from the current page via page.evaluate()."""
    try:
        await page.wait_for_selector(
            '[data-marker="catalog-serp"]',
            timeout=cfg["selector_timeout"],
        )
    except Exception:
        logger.warning("[%s] Catalog container not found, saving debug HTML", city)
        html = await page.content()
        save_debug_html(html, city)
        return None

    await smooth_scroll(page, random.randint(2, 4))

    ads = await page.evaluate(EXTRACT_ADS_JS)
    if ads is None:
        logger.warning("[%s] page.evaluate returned null", city)
        html = await page.content()
        save_debug_html(html, city)
        return None

    return ads


# ---------------------------------------------------------------------------
# Captcha detection
# ---------------------------------------------------------------------------
async def looks_like_captcha(page) -> bool:
    url_lower = page.url.lower()
    url_hints = ["captcha", "challenge", "blocked", "showcaptcha"]
    if any(h in url_lower for h in url_hints):
        return True
    try:
        title = await page.title()
        title_hints = ["доступ ограничен", "проблема с ip", "captcha"]
        if any(h in title.lower() for h in title_hints):
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
async def run(cfg: dict, category_path: str, cities: list[str],
              keywords: list[list[str]], skip: int = 0,
              query: str | None = None):
    global collected_results, shutdown_requested

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if skip > 0:
        logger.info("Skipping first %d cities", skip)
        cities = cities[skip:]

    if not cities:
        logger.error("No cities to process. Check cities.txt")
        return

    logger.info("Starting scan: %d cities, category: %s%s",
                len(cities), category_path,
                f", query: {unquote(query)}" if query else "")

    consecutive_errors = 0

    async with async_playwright() as pw:
        browser, context, page = await create_browser(pw, cfg)

        try:
            await warm_cookies(page, cfg)

            for i, city in enumerate(cities, start=1):
                if shutdown_requested:
                    logger.info("Shutdown requested, stopping after %d cities", i - 1)
                    break

                url = f"https://www.avito.ru/{city}/{category_path}" + (f"?{query}" if query else "")
                logger.info("[%d/%d] %s", i, len(cities), city)

                success = False
                for attempt in range(cfg["max_retries"] + 1):
                    try:
                        resp = await page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=cfg["page_timeout"],
                        )

                        # Captcha check
                        if await looks_like_captcha(page):
                            pause = random.uniform(60, 120)
                            logger.warning("[%s] Captcha detected, pausing %.0fs", city, pause)
                            await asyncio.sleep(pause)
                            continue

                        ads = await extract_ads(page, city, cfg)
                        if ads is None:
                            raise RuntimeError("Failed to extract ads")

                        mine_count = 0
                        for ad in ads:
                            ad["city"] = city
                            ad["is_mine"] = is_mine(ad["ad_title"], keywords)
                            ad["_run_ts"] = run_ts
                            if ad["is_mine"]:
                                mine_count += 1

                        collected_results.extend(ads)
                        save_results(collected_results, city)

                        logger.info(
                            "[%s] %d ads, %d mine (positions: %s)",
                            city,
                            len(ads),
                            mine_count,
                            ", ".join(
                                str(a["ad_position"])
                                for a in ads
                                if a["is_mine"]
                            ) or "—",
                        )

                        consecutive_errors = 0
                        success = True
                        break

                    except Exception as e:
                        logger.warning(
                            "[%s] Attempt %d/%d failed: %s",
                            city, attempt + 1, cfg["max_retries"] + 1, e,
                        )
                        await asyncio.sleep(random.uniform(10, 20))

                if not success:
                    consecutive_errors += 1
                    logger.error("[%s] All retries exhausted", city)
                    if consecutive_errors >= 5:
                        logger.error("5 consecutive errors — stopping early")
                        break

                # Delay between cities
                if i % cfg["long_pause_every"] == 0:
                    pause = random.uniform(cfg["long_pause_min"], cfg["long_pause_max"])
                    logger.info("Long pause: %.1fs", pause)
                    await asyncio.sleep(pause)
                else:
                    await asyncio.sleep(random.uniform(cfg["min_delay"], cfg["max_delay"]))

        finally:
            try:
                await browser.close()
            except Exception:
                pass

    save_results(collected_results)
    print_report(collected_results)
    logger.info("Done. Total records: %d", len(collected_results))


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------
def handle_signal(signum, frame):
    global shutdown_requested
    if shutdown_requested:
        logger.warning("Force quit — saving partial results")
        save_results(collected_results)
        sys.exit(1)
    logger.info("Ctrl+C received — finishing current city, then saving …")
    shutdown_requested = True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Avito position tracker",
        usage="%(prog)s URL [options]",
    )
    parser.add_argument("url", help="Avito category URL, e.g. https://www.avito.ru/all/predlozheniya_uslug/...")
    parser.add_argument("--skip", type=int, default=0, help="Skip first N cities")
    parser.add_argument("--cities", type=str, default=None, help="Path to cities.txt")
    parser.add_argument("--keywords", type=str, default=None, help="Path to keywords.txt")
    parser.add_argument("--config", type=str, default=None, help="Path to config.json (timing settings)")
    parser.add_argument("--debug", action="store_true", help="Run in headed (visible) mode")
    args = parser.parse_args()

    setup_logging()
    signal.signal(signal.SIGINT, handle_signal)

    category_path, query = parse_category_path(args.url)
    cities = load_cities(Path(args.cities) if args.cities else None)
    keywords = load_keywords(Path(args.keywords) if args.keywords else None)

    logger.info("Category: %s", category_path)
    if query:
        logger.info("Search query: %s", unquote(query))
    logger.info("Cities: %d from %s", len(cities), args.cities or "cities.txt")
    logger.info("Keywords: %d groups from %s", len(keywords), args.keywords or "keywords.txt")

    cfg = load_config(Path(args.config) if args.config else None)
    if args.debug:
        cfg["headless"] = False

    asyncio.run(run(cfg, category_path, cities, keywords, skip=args.skip, query=query))


if __name__ == "__main__":
    main()
