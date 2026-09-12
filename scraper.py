"""
Scraping layer. Uses Playwright because marriott.com's search results are
rendered client-side — plain requests/BeautifulSoup won't see prices.

Two things are city-specific and can't be guessed from a URL template alone:
  1. destinationAddress.* params (placeId, lat/long, state, country) — these
     come from Marriott's own autosuggest, so we resolve them once per city
     by actually typing into the search box like a user would.
  2. The exact shape of the hotel results (name / price / link) — Marriott
     is a modern JS app, so results are very likely fetched via an internal
     XHR/fetch call returning JSON rather than baked into static HTML. That's
     far more reliable to parse than CSS class names (which can be
     machine-generated and change on every deploy).

HOW TO FINISH extract_hotels_from_page():
  1. Open the URL you sent me in a normal browser with DevTools open on the
     Network tab, filtered to Fetch/XHR.
  2. Reload the page and look for a request that returns the hotel list
     (something like "findHotels", "availability", "searchResults"...).
  3. Send me that request's URL and a sample of its JSON response.
  I'll wire the parser to that response directly — it'll be far more robust
  than scraping the rendered DOM. Until then, this file has a DOM-scraping
  fallback with best-guess selectors that you'll likely need to adjust by
  inspecting the page (right-click a hotel card -> Inspect).
"""
import asyncio
import re
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

from playwright.async_api import async_playwright, Browser, Page

MARRIOTT_HOME = "https://www.marriott.com/search/default.mi"
SEARCH_BASE = "https://www.marriott.com/search/findHotels.mi"

# Static params copied from the working search URL you sent, kept fixed
# across every query.
FIXED_PARAMS = {
    "childrenCountBox": "0 Children Per Room",
    "childrenCount": "0",
    "clusterCode": "corp",
    "isAdvanceSearch": "false",
    "isNGSF": "false",
    "recordsPerPage": "40",
    "isInternalSearch": "true",
    "vsInitialRequest": "false",
    "searchType": "InCity",
    "searchRadius": "50",
    "singleSearchAutoSuggest": "Unmatched",
    "for-hotels-nearme": "Near",
    "collapseAccordian": "is-hidden",
    "singleSearch": "true",
    "isTransient": "true",
    "initialRequest": "true",
    "flexibleDateSearchRateDisplay": "false",
    "isSearch": "true",
    "isRateCalendar": "true",
    "isHideFlexibleDateCalendar": "false",
    "roomCountBox": "1 Room",
    "roomCount": "1",
    "guestCountBox": "1 Adult Per Room",
    "numAdultsPerRoom": "1",
    "deviceType": "desktop-web",
    "view": "list",
    "isFlexibleDatesOptionSelected": "false",
    "flexibleDateSearch": "false",
    "corporateCode": "MMA",
    "numberOfRooms": "1",
    "corp": "corp",
}


async def resolve_destination(browser: Browser, city_name: str) -> dict:
    """
    Types `city_name` into marriott.com's own search box and captures the
    destinationAddress.* params Marriott resolves it to, by reading the
    request Marriott's own front-end sends when you click the suggestion
    and hit search. Returns a dict ready to merge into query params.
    """
    page: Page = await browser.new_page()
    captured = {}

    async def on_request(request):
        if "findHotels.mi" in request.url and "destinationAddress.city" in request.url:
            # Grab the params straight off marriott's own generated URL.
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(request.url).query)
            for k, v in qs.items():
                if k.startswith("destinationAddress."):
                    captured[k] = v[0]

    page.on("request", on_request)

    await page.goto(MARRIOTT_HOME, wait_until="domcontentloaded")
    # NOTE: selector below is a best guess — confirm/adjust against the live
    # page (Inspect the destination input field).
    search_box = page.locator("input#search-form--input, input[name='destinationAddress.destination']").first
    await search_box.click()
    await search_box.fill(city_name)
    await page.wait_for_timeout(1200)  # let autosuggest render

    suggestion = page.locator("li.destination-suggestion, [role='option']").first
    await suggestion.click()

    submit = page.locator("button[type='submit'], .search-submit").first
    await submit.click()

    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1000)
    await page.close()

    if not captured:
        raise RuntimeError(
            f"Could not resolve destination params for '{city_name}'. "
            "The search-box selector likely needs adjusting to match the "
            "current marriott.com markup."
        )
    return captured


def build_search_url(destination_params: dict, night: str) -> str:
    """night: 'YYYY-MM-DD' — a single-night stay (check-in that date, checkout +1)."""
    checkin = datetime.strptime(night, "%Y-%m-%d")
    checkout = checkin + timedelta(days=1)
    fmt = "%m/%d/%Y"
    iso = "%Y-%m-%d"

    params = dict(FIXED_PARAMS)
    params.update(destination_params)
    params.update({
        "fromToDate_submit": checkout.strftime(fmt),
        "fromDate": checkin.strftime(fmt),
        "toDate": checkout.strftime(fmt),
        "toDateDefaultFormat": checkout.strftime(fmt),
        "fromDateDefaultFormat": checkin.strftime(fmt),
        "t-start": checkin.strftime(iso),
        "t-end": checkout.strftime(iso),
        "lengthOfStay": "1",
        "fromToDate": checkin.strftime(fmt),
    })
    return f"{SEARCH_BASE}?{urlencode(params)}"


async def extract_hotels_from_page(page: Page) -> list:
    """
    Best-effort DOM extraction. Replace/extend once we have the real JSON
    endpoint (see module docstring) — that will be much less fragile than
    these selectors.
    """
    hotels = []
    cards = page.locator("[data-testid='hotel-card'], .hotel-card, li.hotel-name-card")
    count = await cards.count()
    for i in range(count):
        card = cards.nth(i)
        try:
            name = (await card.locator("h2, h3, .hotel-name").first.inner_text()).strip()
        except Exception:
            continue
        try:
            price_text = await card.locator("[class*='price'], .rate-value").first.inner_text()
            price = float(re.sub(r"[^\d.]", "", price_text.replace(",", "")))
        except Exception:
            price = None
        try:
            link = await card.locator("a").first.get_attribute("href")
            if link and link.startswith("/"):
                link = "https://www.marriott.com" + link
        except Exception:
            link = None

        hotels.append({"name": name, "price": price, "link": link})
    return hotels


async def search_night(browser: Browser, destination_params: dict, night: str) -> list:
    url = build_search_url(destination_params, night)
    page = await browser.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(1500)
        return await extract_hotels_from_page(page)
    finally:
        await page.close()


def hotel_key(hotel: dict) -> str:
    """Stable-ish key for dedup: prefer the link (usually contains a property
    code), fall back to the normalized name."""
    if hotel.get("link"):
        m = re.search(r"/hotels/([a-zA-Z0-9\-]+)", hotel["link"])
        if m:
            return m.group(1)
    return re.sub(r"\s+", " ", hotel.get("name", "")).strip().lower()
