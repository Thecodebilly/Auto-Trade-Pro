"""Vehicle inventory web scraper for AutoTrade Pro.

Multi-strategy extraction pipeline:
  0. Typesense / InstantSearch adapter detection (used by many Toyota, Honda,
     Nissan and other OEM dealer sites powered by Dealer.com / DealerSocket /
     CDK Roadster variants that embed TypesenseInstantSearchAdapter)
  1. JSON-LD structured data (Schema.org Car / Vehicle / ItemList)
  2. Embedded JSON in <script> tags (React/Vue/SPA window state patterns)
  3. XML / JSON feed detection (direct inventory feed URLs)
  4. Common automotive HTML card patterns (CDK, DealerSocket, Dealer.com, etc.)
  5. Common REST/API endpoint discovery
  6. AI fallback — uses dealer's OpenAI key to parse arbitrary HTML

Returns a normalised list of vehicle dicts suitable for the inventory table.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

import requests

try:
    from playwright.sync_api import (
        Error as PlaywrightError,
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )

    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover - optional fallback for minimal installs
    PlaywrightError = Exception
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False

try:
    from bs4 import BeautifulSoup

    BS4_AVAILABLE = True
except ImportError:  # pragma: no cover
    BS4_AVAILABLE = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

SCRAPE_TIMEOUT = 20
MAX_VEHICLES = 5000  # effectively unlimited; set per-call via max_vehicles param
MAX_PAGES = 200
PLAYWRIGHT_NAVIGATION_TIMEOUT_MS = 25_000
PLAYWRIGHT_NETWORK_IDLE_TIMEOUT_MS = 4_000
PLAYWRIGHT_SETTLE_MS = 800
PLAYWRIGHT_SCROLL_STEPS = 36
PLAYWRIGHT_MAX_RESPONSE_BYTES = 25_000_000
PLAYWRIGHT_SMALL_JSON_BYTES = 2_000_000
PLAYWRIGHT_DISCOVERY_LIMIT = 8
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_BAD_IMAGE_MARKERS = (
    "coming-soon",
    "coming_soon",
    "default",
    "image-not-available",
    "logo",
    "no-image",
    "no-photo",
    "no_image",
    "no_photo",
    "noimage",
    "nophoto",
    "notavailable",
    "placeholder",
    "photo-not-available",
    "transparent",
)
_RENDER_IMAGE_MARKERS = (
    "jellies",
    "media.dealeralchemist.com",
    "stock-photo",
    "stockphoto",
)
_REAL_IMAGE_MARKERS = (
    "assets.cai-media-management.com",
    "common-vehicle-media",
    "inventory",
    "photo",
    "vehicle-media",
)
_PLAYWRIGHT_BROWSER_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
)
_INVENTORY_RESPONSE_MARKERS = (
    "api",
    "ajax",
    "cars",
    "graphql",
    "inventory",
    "listing",
    "listings",
    "search",
    "srp",
    "vehicle",
    "vehicles",
)
_INVENTORY_CATEGORY_MARKERS = (
    "all inventory",
    "all vehicles",
    "all cars",
    "inventory",
    "new inventory",
    "new vehicles",
    "new cars",
    "pre-owned",
    "preowned",
    "used inventory",
    "used vehicles",
    "used cars",
    "certified inventory",
    "certified vehicles",
)
_NON_INVENTORY_LINK_MARKERS = (
    "appointment",
    "blog",
    "careers",
    "contact",
    "finance",
    "parts",
    "privacy",
    "service",
    "special",
    "trade",
)


def scrape_inventory(
    url: str,
    *,
    openai_api_key: str = "",
    openai_model: str = "gpt-4.1-mini",
    max_vehicles: int = MAX_VEHICLES,
) -> dict[str, Any]:
    """Scrape vehicle inventory from *url*.

    Returns::

        {
            "vehicles": [...],   # normalised vehicle dicts
            "total": int,
            "source": str,       # which strategy succeeded
            "scraped_url": str,
            "errors": [...],
        }
    """
    errors: list[str] = []

    # Fast paths for direct feeds/search adapters. These already expose the full
    # inventory without needing a rendered browser session.
    html0, final_url0, err0 = _fetch_page(url)
    if err0:
        errors.append(err0)
    else:
        feed_vehicles, feed_source = _extract_page_vehicles(
            html0,
            final_url0,
            openai_api_key="",
            openai_model=openai_model,
            allow_ai=False,
            feeds_only=True,
        )
        if feed_vehicles:
            return _scrape_result(
                feed_vehicles,
                feed_source,
                url,
                errors,
                max_vehicles,
            )

        ts_result = _try_typesense(html0, url, max_vehicles)
        if ts_result is not None:
            ts_vehicles, ts_err = ts_result
            if ts_err:
                errors.append(ts_err)
            if ts_vehicles:
                return _scrape_result(
                    ts_vehicles,
                    "typesense",
                    url,
                    errors,
                    max_vehicles,
                )

    # Rendered browser pass: handles SPA inventories, infinite scroll,
    # client-side search APIs, load-more buttons, and homepages that link to
    # separate new/used inventory pages.
    browser_vehicles, browser_source, browser_errors = _scrape_with_playwright(
        url,
        openai_api_key=openai_api_key,
        openai_model=openai_model,
        max_vehicles=max_vehicles,
    )
    if browser_vehicles:
        api_vehicles, _api_err = _try_api_endpoints(url)
        if api_vehicles:
            browser_vehicles.extend(api_vehicles)
            browser_source = _combine_sources(browser_source, "api_discovery")
        return _scrape_result(
            browser_vehicles,
            browser_source,
            url,
            errors,
            max_vehicles,
        )

    # HTTP fallback for local/dev environments where Playwright or Chromium is
    # not available. This keeps existing structured-data parsing useful, but the
    # browser path above is the primary path for dealership websites.
    static_vehicles, static_source, static_errors = _scrape_http_pages(
        url,
        first_page=(html0, final_url0) if not err0 else None,
        openai_api_key=openai_api_key,
        openai_model=openai_model,
        max_vehicles=max_vehicles,
    )
    errors.extend(static_errors)

    if static_vehicles:
        api_vehicles, _api_err = _try_api_endpoints(url)
        if api_vehicles:
            static_vehicles.extend(api_vehicles)
            static_source = _combine_sources(static_source, "api_discovery")
        return _scrape_result(
            static_vehicles,
            static_source,
            url,
            errors,
            max_vehicles,
        )

    errors.extend(browser_errors)

    api_vehicles, api_err = _try_api_endpoints(url)
    if api_vehicles:
        return _scrape_result(
            api_vehicles,
            "api_discovery",
            url,
            errors,
            max_vehicles,
        )
    if api_err:
        errors.append(api_err)

    return _scrape_result([], "none", url, errors, max_vehicles)


def _scrape_http_pages(
    url: str,
    *,
    first_page: tuple[str, str] | None,
    openai_api_key: str,
    openai_model: str,
    max_vehicles: int,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    vehicles: list[dict[str, Any]] = []
    errors: list[str] = []
    source = "none"
    seen_keys: set[str] = set()
    pages_tried = 0

    _prefetched = first_page
    current_url: str | None = url
    while current_url and pages_tried < MAX_PAGES and len(vehicles) < max_vehicles:
        if _prefetched is not None:
            html, final_url = _prefetched
            _prefetched = None
        else:
            html, final_url, err = _fetch_page(current_url)
            if err:
                errors.append(err)
                break
        pages_tried += 1

        page_vehicles, page_source = _extract_page_vehicles(
            html,
            final_url,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
            allow_ai=pages_tried == 1,
        )

        if source == "none" and page_source != "none":
            source = page_source

        _append_unique_vehicles(vehicles, page_vehicles, seen_keys, max_vehicles)

        # Detect next-page URL (only for HTML-based scraping)
        if (
            page_source in ("html_patterns", "json_ld", "embedded_json")
            and BS4_AVAILABLE
        ):
            current_url = _find_next_page(html, final_url)
        else:
            current_url = None

    return vehicles, source, errors


def _extract_page_vehicles(
    html: str,
    final_url: str,
    *,
    openai_api_key: str,
    openai_model: str,
    allow_ai: bool,
    feeds_only: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    page_vehicles: list[dict[str, Any]] = []
    page_source = "none"

    if _looks_like_json_feed(html):
        feed_vehicles = _extract_json_feed(html, final_url)
        if feed_vehicles:
            return feed_vehicles, "json_feed"
    if _looks_like_xml_feed(html):
        feed_vehicles = _extract_xml_feed(html, final_url)
        if feed_vehicles:
            return feed_vehicles, "xml_feed"

    if feeds_only:
        return [], "none"

    if BS4_AVAILABLE:
        ld_vehicles = _extract_json_ld(html)
        if ld_vehicles:
            page_vehicles = ld_vehicles
            page_source = "json_ld"

    if not page_vehicles and BS4_AVAILABLE:
        emb_vehicles = _extract_embedded_json(html, base_url=final_url)
        if emb_vehicles:
            page_vehicles = emb_vehicles
            page_source = "embedded_json"

    if not page_vehicles and BS4_AVAILABLE:
        html_vehicles = _extract_html_patterns(html, base_url=final_url)
        if html_vehicles:
            page_vehicles = html_vehicles
            page_source = "html_patterns"

    if not page_vehicles and allow_ai and openai_api_key and BS4_AVAILABLE:
        ai_vehicles = _ai_extract(html, final_url, openai_api_key, openai_model)
        if ai_vehicles:
            page_vehicles = ai_vehicles
            page_source = "ai"

    return page_vehicles, page_source


def _scrape_with_playwright(
    url: str,
    *,
    openai_api_key: str,
    openai_model: str,
    max_vehicles: int,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    if not PLAYWRIGHT_AVAILABLE or sync_playwright is None:
        return (
            [],
            "none",
            [
                "Playwright is not installed; install dependencies and run "
                "`python -m playwright install chromium` to enable rendered scraping."
            ],
        )

    vehicles: list[dict[str, Any]] = []
    response_vehicles: list[dict[str, Any]] = []
    errors: list[str] = []
    sources: list[str] = []
    seen_vehicle_keys: set[str] = set()
    seen_pages: set[str] = set()
    queued_pages: set[str] = {_canonical_page_url(url)}
    page_queue: list[str] = [url]

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=list(_PLAYWRIGHT_BROWSER_ARGS),
            )
            try:
                context = browser.new_context(
                    user_agent=_USER_AGENT,
                    viewport={"width": 1440, "height": 1200},
                    locale="en-US",
                    timezone_id="America/New_York",
                )
                page = context.new_page()

                def capture_response(response: Any) -> None:
                    found = _vehicles_from_playwright_response(response)
                    if found:
                        response_vehicles.extend(found)

                page.on("response", capture_response)

                while (
                    page_queue
                    and len(vehicles) < max_vehicles
                    and len(seen_pages) < MAX_PAGES
                ):
                    current_url = page_queue.pop(0)
                    canonical = _canonical_page_url(current_url)
                    if canonical in seen_pages:
                        continue
                    seen_pages.add(canonical)

                    ok, warning = _goto_playwright_page(page, current_url)
                    if warning:
                        errors.append(warning)
                    if not ok:
                        continue

                    _expand_rendered_inventory(page)

                    final_url = page.url
                    html = page.content()

                    ts_result = _try_typesense(
                        html,
                        final_url,
                        max(1, max_vehicles - len(vehicles)),
                    )
                    if ts_result is not None:
                        ts_vehicles, ts_err = ts_result
                        if ts_err:
                            errors.append(ts_err)
                        if ts_vehicles:
                            _append_unique_vehicles(
                                vehicles,
                                ts_vehicles,
                                seen_vehicle_keys,
                                max_vehicles,
                            )
                            sources.append("playwright_typesense")

                    response_batch = response_vehicles[:]
                    response_vehicles.clear()
                    if response_batch:
                        _append_unique_vehicles(
                            vehicles,
                            response_batch,
                            seen_vehicle_keys,
                            max_vehicles,
                        )
                        sources.append("playwright_api")

                    state_vehicles = _extract_playwright_state(page, final_url)
                    if state_vehicles:
                        _append_unique_vehicles(
                            vehicles,
                            state_vehicles,
                            seen_vehicle_keys,
                            max_vehicles,
                        )
                        sources.append("playwright_state")

                    page_vehicles, page_source = _extract_page_vehicles(
                        html,
                        final_url,
                        openai_api_key=openai_api_key,
                        openai_model=openai_model,
                        allow_ai=len(seen_pages) == 1,
                    )
                    if page_vehicles:
                        _append_unique_vehicles(
                            vehicles,
                            page_vehicles,
                            seen_vehicle_keys,
                            max_vehicles,
                        )
                        sources.append(f"playwright_{page_source}")

                    if len(vehicles) >= max_vehicles:
                        break

                    next_url = _find_next_page(html, final_url)
                    if next_url:
                        _queue_inventory_page(
                            next_url,
                            page_queue,
                            queued_pages,
                            front=True,
                        )

                    for discovered_url in _discover_inventory_pages(html, final_url):
                        if len(queued_pages) >= PLAYWRIGHT_DISCOVERY_LIMIT + len(
                            seen_pages
                        ):
                            break
                        _queue_inventory_page(
                            discovered_url,
                            page_queue,
                            queued_pages,
                        )
            finally:
                browser.close()
    except Exception as exc:
        return [], "none", [f"Playwright browser scrape failed: {exc}"]

    return vehicles, _combine_sources(*sources), errors


def _goto_playwright_page(page: Any, url: str) -> tuple[bool, str]:
    warning = ""
    try:
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PLAYWRIGHT_NAVIGATION_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        warning = f"Playwright timed out waiting for initial load: {url}"
    except PlaywrightError as exc:
        return False, f"Playwright could not load {url}: {exc}"

    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=PLAYWRIGHT_NETWORK_IDLE_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        pass
    except PlaywrightError:
        pass

    try:
        page.wait_for_timeout(PLAYWRIGHT_SETTLE_MS)
    except PlaywrightError:
        return False, f"Playwright page became unavailable after loading {url}"

    return True, warning


def _expand_rendered_inventory(page: Any) -> None:
    stable_rounds = 0
    previous_signature = ""

    for _step in range(PLAYWRIGHT_SCROLL_STEPS):
        try:
            page.evaluate(
                "() => window.scrollTo({ top: document.body.scrollHeight, behavior: 'instant' })"
            )
            page.wait_for_timeout(PLAYWRIGHT_SETTLE_MS)
            clicked = _click_load_more_control(page)
            if clicked:
                page.wait_for_timeout(PLAYWRIGHT_SETTLE_MS)
                try:
                    page.wait_for_load_state(
                        "networkidle",
                        timeout=PLAYWRIGHT_NETWORK_IDLE_TIMEOUT_MS,
                    )
                except PlaywrightTimeoutError:
                    pass
            signature = _rendered_inventory_signature(page)
        except PlaywrightError:
            return

        if clicked or signature != previous_signature:
            stable_rounds = 0
        else:
            stable_rounds += 1
        previous_signature = signature
        if stable_rounds >= 3:
            break


def _click_load_more_control(page: Any) -> str:
    return str(
        page.evaluate(
            r"""
            () => {
              const matcher = /((load|show|view|see)\s+(all\s+)?(more|additional))|(more\s+(vehicles|inventory|results|cars|listings))|show\s+all/i;
              const selectors = 'button,a,[role="button"],input[type="button"],input[type="submit"]';
              const nodes = Array.from(document.querySelectorAll(selectors));
              for (const el of nodes) {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                if (!rect.width || !rect.height || style.display === 'none' || style.visibility === 'hidden') continue;
                if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                const text = [
                  el.innerText || '',
                  el.value || '',
                  el.getAttribute('aria-label') || '',
                  el.getAttribute('title') || ''
                ].join(' ').replace(/\s+/g, ' ').trim();
                if (!text || !matcher.test(text)) continue;
                el.scrollIntoView({ block: 'center', inline: 'center' });
                el.click();
                return text.slice(0, 80);
              }
              return '';
            }
            """
        )
        or ""
    )


def _rendered_inventory_signature(page: Any) -> str:
    return str(
        page.evaluate(
            r"""
            () => {
              const text = document.body ? document.body.innerText || '' : '';
              const vins = text.match(/\b[A-HJ-NPR-Z0-9]{17}\b/g) || [];
              const vehicleTitles = text.match(/\b(19[6-9]\d|20[0-4]\d)\s+[A-Z][A-Za-z-]+\s+[A-Z0-9][A-Za-z0-9-]+/g) || [];
              const cards = document.querySelectorAll([
                '[data-vin]',
                '[data-stock-number]',
                '[data-vehicle-id]',
                '[data-listing-id]',
                '[class*="vehicle" i]',
                '[class*="inventory" i]',
                '[class*="listing" i]',
                '[class*="srp" i]'
              ].join(',')).length;
              return [
                document.body ? document.body.scrollHeight : 0,
                cards,
                vins.length,
                vehicleTitles.length
              ].join(':');
            }
            """
        )
        or ""
    )


def _vehicles_from_playwright_response(response: Any) -> list[dict[str, Any]]:
    try:
        status = int(response.status)
    except Exception:
        status = 0
    if status >= 400:
        return []

    url = str(getattr(response, "url", "") or "")
    lower_url = url.lower()
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("content-type", "")).lower()
    length = _int_or_none(headers.get("content-length")) or 0
    inventory_url = any(marker in lower_url for marker in _INVENTORY_RESPONSE_MARKERS)

    if "json" not in content_type and not inventory_url:
        return []
    if length and length > PLAYWRIGHT_MAX_RESPONSE_BYTES:
        return []
    if (
        "json" in content_type
        and not inventory_url
        and length
        and length > PLAYWRIGHT_SMALL_JSON_BYTES
    ):
        return []

    try:
        data = response.json()
    except Exception:
        try:
            text = response.text()
        except Exception:
            return []
        if not _looks_like_json_feed(text):
            return []
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return []

    return _walk_json_for_vehicles(data, url)


def _extract_playwright_state(page: Any, base_url: str) -> list[dict[str, Any]]:
    try:
        state_blobs = page.evaluate(
            r"""
            () => {
              const names = [
                '__NEXT_DATA__',
                '__NUXT__',
                '__INITIAL_STATE__',
                '__INITIAL_DATA__',
                '__PRELOADED_STATE__',
                '__REDUX_STATE__',
                '__APOLLO_STATE__',
                '__RELAY_STORE__',
                '__remixContext',
                'DDC'
              ];
              const blobs = [];
              for (const name of names) {
                const value = window[name];
                if (value === undefined || value === null) continue;
                try {
                  const text = JSON.stringify(value);
                  if (text && text.length < 8000000) blobs.push(text);
                } catch (_err) {}
              }
              return blobs;
            }
            """
        )
    except PlaywrightError:
        return []

    vehicles: list[dict[str, Any]] = []
    if not isinstance(state_blobs, list):
        return vehicles
    for blob in state_blobs:
        try:
            data = json.loads(str(blob))
        except (json.JSONDecodeError, ValueError):
            continue
        vehicles.extend(_walk_json_for_vehicles(data, base_url))
    return vehicles


def _discover_inventory_pages(html: str, current_url: str) -> list[str]:
    if not BS4_AVAILABLE:
        return []
    soup = BeautifulSoup(html, "html.parser")
    current = urllib.parse.urlparse(current_url)
    origin = f"{current.scheme}://{current.netloc}"
    discovered: list[str] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urllib.parse.urljoin(current_url, href)
        parsed = urllib.parse.urlparse(absolute)
        if f"{parsed.scheme}://{parsed.netloc}" != origin:
            continue
        clean = urllib.parse.urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, "")
        )
        if clean in seen:
            continue
        text = anchor.get_text(" ", strip=True)
        if not _looks_like_inventory_category_link(text, clean):
            continue
        seen.add(clean)
        discovered.append(clean)
        if len(discovered) >= PLAYWRIGHT_DISCOVERY_LIMIT:
            break

    return discovered


def _looks_like_inventory_category_link(text: str, url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path).lower()
    label = f"{text} {path}".lower().replace("_", "-")

    if any(marker in label for marker in _NON_INVENTORY_LINK_MARKERS):
        return False
    if not any(marker in label for marker in _INVENTORY_CATEGORY_MARKERS):
        return False
    if _VIN_RE.search(url.upper()):
        return False
    if re.search(r"/(?:new|used|certified|pre-owned)-\d{4}-", path):
        return False
    if re.search(r"\b(19[6-9]\d|20[0-4]\d)\b", path) and len(
        [part for part in path.split("/") if part]
    ) > 2:
        return False
    return True


def _queue_inventory_page(
    url: str,
    queue: list[str],
    queued_pages: set[str],
    *,
    front: bool = False,
) -> None:
    canonical = _canonical_page_url(url)
    if canonical in queued_pages:
        return
    queued_pages.add(canonical)
    if front:
        queue.insert(0, url)
    else:
        queue.append(url)


def _canonical_page_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    return urllib.parse.urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            "",
            urllib.parse.urlencode(sorted(query)),
            "",
        )
    )


def _append_unique_vehicles(
    target: list[dict[str, Any]],
    vehicles: list[dict[str, Any]],
    seen_keys: set[str],
    max_vehicles: int,
) -> None:
    for vehicle in vehicles:
        if not _looks_like_vehicle(vehicle):
            continue
        key = _vehicle_key(vehicle)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        target.append(vehicle)
        if len(target) >= max_vehicles:
            break


def _scrape_result(
    vehicles: list[dict[str, Any]],
    source: str,
    scraped_url: str,
    errors: list[str],
    max_vehicles: int,
) -> dict[str, Any]:
    normalised: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for raw in vehicles:
        vehicle = _normalise(raw)
        if not _looks_like_vehicle(vehicle):
            continue
        key = _vehicle_key(vehicle)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        normalised.append(vehicle)
        if len(normalised) >= max_vehicles:
            break

    return {
        "vehicles": normalised,
        "total": len(normalised),
        "source": source or "none",
        "scraped_url": scraped_url,
        "errors": errors,
    }


def _combine_sources(*sources: str) -> str:
    ordered: list[str] = []
    seen: set[str] = set()
    for source in sources:
        if not source or source == "none" or source in seen:
            continue
        seen.add(source)
        ordered.append(source)
    return "+".join(ordered) if ordered else "none"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _fetch_page(url: str) -> tuple[str, str, str]:
    """Return (html, final_url, error).  error is '' on success."""
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    try:
        resp = requests.get(
            url,
            headers=headers,
            timeout=SCRAPE_TIMEOUT,
            allow_redirects=True,
            stream=False,
        )
        resp.raise_for_status()
        # Detect encoding
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text, resp.url, ""
    except requests.RequestException as exc:
        return "", url, f"HTTP error fetching {url}: {exc}"


def _looks_like_json_feed(html: str) -> bool:
    stripped = html.lstrip()
    return stripped.startswith(("{", "["))


def _looks_like_xml_feed(html: str) -> bool:
    stripped = html.lstrip()
    return stripped.startswith("<?xml") or (
        "<inventory" in stripped[:200].lower() or "<vehicles" in stripped[:200].lower()
    )


# ---------------------------------------------------------------------------
# Strategy: direct JSON feed
# ---------------------------------------------------------------------------


def _extract_json_feed(html: str, base_url: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(html)
    except (json.JSONDecodeError, ValueError):
        return []

    vehicles = _walk_json_for_vehicles(data, base_url)
    return vehicles


def _walk_json_for_vehicles(
    data: Any, base_url: str, depth: int = 0
) -> list[dict[str, Any]]:
    if depth > 6:
        return []
    vehicles: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data[:MAX_VEHICLES]:
            if isinstance(item, dict):
                mapped = _map_generic_json_vehicle(item, base_url)
                if _looks_like_vehicle(mapped):
                    vehicles.append(mapped)
                else:
                    vehicles.extend(_walk_json_for_vehicles(item, base_url, depth + 1))
            elif isinstance(item, (dict, list)):
                vehicles.extend(_walk_json_for_vehicles(item, base_url, depth + 1))
        return vehicles
    if isinstance(data, dict):
        # Check if the dict itself is a vehicle
        mapped = _map_generic_json_vehicle(data, base_url)
        if _looks_like_vehicle(mapped):
            return [mapped]
        # Look for known container keys
        known_container_ids: set[int] = set()
        for key in (
            "vehicles",
            "inventory",
            "listings",
            "results",
            "data",
            "items",
            "Vehicles",
            "Inventory",
            "Results",
            "Data",
            "vehicle_list",
            "vehicleList",
            "VehicleList",
            "cars",
            "Cars",
            "docs",
            "hits",
            "records",
            "rows",
            "inventoryItems",
            "inventory_items",
            "vehicleItems",
            "vehicle_items",
            "vehicleResults",
            "vehicle_results",
            "searchResults",
            "search_results",
            "srpVehicles",
            "srp_vehicles",
        ):
            if key in data and isinstance(data[key], (list, dict)):
                known_container_ids.add(id(data[key]))
                found = _walk_json_for_vehicles(data[key], base_url, depth + 1)
                if found:
                    vehicles.extend(found)
        # Recurse into all values
        for val in data.values():
            if id(val) in known_container_ids:
                continue
            if isinstance(val, (dict, list)):
                found = _walk_json_for_vehicles(val, base_url, depth + 1)
                if found:
                    vehicles.extend(found)
    return vehicles


# ---------------------------------------------------------------------------
# Strategy: XML feed
# ---------------------------------------------------------------------------


def _extract_xml_feed(html: str, base_url: str) -> list[dict[str, Any]]:
    # Simple regex-based XML extraction to avoid lxml dependency
    vehicles: list[dict[str, Any]] = []

    # Find vehicle blocks
    vehicle_blocks = re.findall(
        r"<(?:vehicle|Vehicle|car|Car|listing|Listing|unit|Unit)([\s>].*?)</(?:vehicle|Vehicle|car|Car|listing|Listing|unit|Unit)>",
        html,
        re.DOTALL | re.IGNORECASE,
    )

    for block in vehicle_blocks[:MAX_VEHICLES]:
        v: dict[str, Any] = {}
        for tag, field in [
            ("vin", "vin"),
            ("VIN", "vin"),
            ("year", "year"),
            ("Year", "year"),
            ("modelYear", "year"),
            ("make", "make"),
            ("Make", "make"),
            ("model", "model"),
            ("Model", "model"),
            ("trim", "trim"),
            ("Trim", "trim"),
            ("price", "price"),
            ("Price", "price"),
            ("askingPrice", "price"),
            ("mileage", "mileage"),
            ("Mileage", "mileage"),
            ("odometer", "mileage"),
            ("stockNumber", "stock_number"),
            ("StockNumber", "stock_number"),
            ("stock", "stock_number"),
            ("exteriorColor", "ext_color"),
            ("ExteriorColor", "ext_color"),
            ("color", "ext_color"),
            ("bodyStyle", "body_style"),
            ("BodyStyle", "body_style"),
            ("style", "body_style"),
            ("transmission", "transmission"),
            ("engine", "engine"),
            ("description", "description"),
            ("Description", "description"),
        ]:
            match = re.search(
                rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.DOTALL | re.IGNORECASE
            )
            if match:
                v[field] = match.group(1).strip()

        # Images
        images = re.findall(
            r"<(?:image|Image|photo|Photo|img)[^>]*>\s*(https?://[^\s<]+)\s*</(?:image|Image|photo|Photo|img)>",
            block,
        )
        if images:
            v["images"] = images

        if _looks_like_vehicle(v):
            vehicles.append(v)

    return vehicles


# ---------------------------------------------------------------------------
# Strategy 1: JSON-LD
# ---------------------------------------------------------------------------


def _extract_json_ld(html: str) -> list[dict[str, Any]]:
    if not BS4_AVAILABLE:
        return []
    soup = BeautifulSoup(html, "html.parser")
    vehicles: list[dict[str, Any]] = []

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        if isinstance(data, list):
            for item in data:
                v = _map_schema_org(item)
                if v:
                    vehicles.append(v)
        elif isinstance(data, dict):
            graph_type = str(data.get("@type", "")).lower()
            if graph_type in ("car", "vehicle", "motorvehicle"):
                v = _map_schema_org(data)
                if v:
                    vehicles.append(v)
            elif graph_type == "itemlist":
                for item in data.get("itemListElement", []):
                    v = _map_schema_org(item.get("item", item))
                    if v:
                        vehicles.append(v)
            elif "@graph" in data:
                for item in data["@graph"]:
                    v = _map_schema_org(item)
                    if v:
                        vehicles.append(v)
            else:
                # Try to find nested vehicle data
                for val in data.values():
                    if isinstance(val, list):
                        for item in val:
                            v = _map_schema_org(item)
                            if v:
                                vehicles.append(v)

    return vehicles


def _map_schema_org(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    obj_type = str(data.get("@type", "")).lower()
    if obj_type not in ("car", "vehicle", "motorvehicle", "product", ""):
        return None

    v: dict[str, Any] = {}

    # Name parsing: "2022 Toyota Camry SE"
    name = str(data.get("name", "") or "")
    if name:
        parsed = _parse_vehicle_title(name)
        v.update(parsed)

    v["vin"] = str(data.get("vehicleIdentificationNumber", "") or "")
    v["year"] = v.get("year") or _int_or_none(
        data.get("modelDate") or data.get("vehicleModelDate")
    )
    v["make"] = str(
        data.get("brand", {}).get("name", "")
        if isinstance(data.get("brand"), dict)
        else data.get("brand", "")
    ) or v.get("make", "")
    v["model"] = str(data.get("model", "") or "") or v.get("model", "")
    v["body_style"] = str(data.get("bodyType", "") or "")
    v["mileage"] = _int_or_none(
        (data.get("mileageFromOdometer") or {}).get("value")
        if isinstance(data.get("mileageFromOdometer"), dict)
        else data.get("mileageFromOdometer")
    )
    v["ext_color"] = str(data.get("color", "") or "")
    v["transmission"] = str(data.get("vehicleTransmission", "") or "")
    v["drivetrain"] = str(data.get("driveWheelConfiguration", "") or "")
    v["engine"] = (
        str(
            data.get("vehicleEngine", {}).get("name", "")
            if isinstance(data.get("vehicleEngine"), dict)
            else ""
        )
        or ""
    )
    v["description"] = str(data.get("description", "") or "")

    # Price from offers
    offers = data.get("offers", {})
    if isinstance(offers, list) and offers:
        offers = offers[0]
    if isinstance(offers, dict):
        v["price"] = _int_or_none(offers.get("price"))
    elif isinstance(offers, (int, float, str)):
        v["price"] = _int_or_none(offers)

    # Images
    img = data.get("image", "")
    if isinstance(img, str) and img.startswith("http"):
        v["images"] = [img]
    elif isinstance(img, list):
        v["images"] = [i for i in img if isinstance(i, str) and i.startswith("http")]

    # URL
    v["detail_url"] = str(data.get("url", "") or "")

    if not _looks_like_vehicle(v):
        return None
    return v


# ---------------------------------------------------------------------------
# Strategy 2: Embedded JSON in <script> tags
# ---------------------------------------------------------------------------

# Patterns that commonly hold inventory arrays
_WINDOW_PATTERNS = [
    re.compile(r"window\.__INVENTORY\s*=\s*(\[.*?\]);", re.DOTALL),
    re.compile(r"window\.inventory\s*=\s*(\[.*?\]);", re.DOTALL),
    re.compile(r"window\.vehicles\s*=\s*(\[.*?\]);", re.DOTALL),
    re.compile(r"window\.vehicleData\s*=\s*(\[.*?\]);", re.DOTALL),
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});", re.DOTALL),
    re.compile(r"window\.__NEXT_DATA__\s*=\s*(\{.*?\});", re.DOTALL),
    re.compile(r"window\.pageData\s*=\s*(\{.*?\});", re.DOTALL),
    re.compile(r"window\.DDC\.dataLayer\s*=\s*(\{.*?\});", re.DOTALL),
    re.compile(r"window\.spaCache\s*=\s*(\{.*?\});", re.DOTALL),
    re.compile(
        r'"vehicles"\s*:\s*(\[(?:[^\[\]]|\[(?:[^\[\]]|\[[^\[\]]*\])*\])*\])', re.DOTALL
    ),
    re.compile(
        r'"inventory"\s*:\s*(\[(?:[^\[\]]|\[(?:[^\[\]]|\[[^\[\]]*\])*\])*\])', re.DOTALL
    ),
    re.compile(
        r'"listings"\s*:\s*(\[(?:[^\[\]]|\[(?:[^\[\]]|\[[^\[\]]*\])*\])*\])', re.DOTALL
    ),
    re.compile(
        r'"results"\s*:\s*(\[(?:[^\[\]]|\[(?:[^\[\]]|\[[^\[\]]*\])*\])*\])', re.DOTALL
    ),
]


def _extract_embedded_json(html: str, base_url: str) -> list[dict[str, Any]]:
    if not BS4_AVAILABLE:
        return []
    soup = BeautifulSoup(html, "html.parser")
    vehicles: list[dict[str, Any]] = []

    script_texts: list[str] = []
    for tag in soup.find_all("script"):
        text = tag.string or ""
        if text and len(text) > 100:
            script_texts.append(text)

    for script in script_texts:
        for pattern in _WINDOW_PATTERNS:
            match = pattern.search(script)
            if not match:
                continue
            try:
                data = json.loads(match.group(1))
            except (json.JSONDecodeError, ValueError):
                # Try to truncate and parse again
                try:
                    data = json.loads(_best_effort_json(match.group(1)))
                except Exception:
                    continue
            found = _walk_json_for_vehicles(data, base_url)
            if found:
                vehicles.extend(found)
                break

    # Also scan large JSON objects embedded inline as data- attributes
    for tag in soup.find_all(attrs={"data-inventory": True}):
        try:
            data = json.loads(tag["data-inventory"])
            vehicles.extend(_walk_json_for_vehicles(data, base_url))
        except Exception:
            pass

    return vehicles


def _best_effort_json(raw: str) -> str:
    """Attempt to close unfinished JSON by counting brackets."""
    opens = raw.count("{") - raw.count("}")
    closes = raw.count("[") - raw.count("]")
    return raw + ("}" * max(opens, 0)) + ("]" * max(closes, 0))


# ---------------------------------------------------------------------------
# Strategy 3: HTML card patterns
# ---------------------------------------------------------------------------

# CSS class fragments that commonly identify vehicle listing cards
_CARD_CLASSES = [
    "vehicle-card",
    "vehicle_card",
    "vehicleCard",
    "inventory-card",
    "inventory_card",
    "inventoryCard",
    "inventory-item",
    "inventory_item",
    "inventoryItem",
    "car-card",
    "car_card",
    "carCard",
    "result-item",
    "result_item",
    "resultItem",
    "listing-item",
    "listing_item",
    "listingItem",
    "search-result",
    "searchResult",
    "srp-listing",
    "srpListing",
    "vehicle-tile",
    "vehicleTile",
    "car-listing",
    "carListing",
    "used-car-tile",
    "used_car_tile",
]

_PRICE_RE = re.compile(r"\$\s*([\d,]+)")
_MILEAGE_RE = re.compile(r"([\d,]+)\s*(?:mi|miles|kilometer|km)", re.IGNORECASE)
_VIN_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")
_YEAR_RE = re.compile(r"\b(19[6-9]\d|20[0-4]\d)\b")
_TITLE_RE = re.compile(
    r"\b(20\d\d|19\d\d)\s+([A-Z][a-zA-Z\-]+)\s+([A-Z][a-zA-Z0-9\-/ ]+)"
)


def _extract_html_patterns(html: str, base_url: str) -> list[dict[str, Any]]:
    if not BS4_AVAILABLE:
        return []
    soup = BeautifulSoup(html, "html.parser")
    vehicles: list[dict[str, Any]] = []

    # Build a list of candidate card elements
    candidates = []
    for cls in _CARD_CLASSES:
        found = soup.find_all(class_=re.compile(re.escape(cls), re.IGNORECASE))
        candidates.extend(found)

    # Also try data attributes
    candidates.extend(soup.find_all(attrs={"data-vin": True}))
    candidates.extend(soup.find_all(attrs={"data-vehicle-id": True}))
    candidates.extend(soup.find_all(attrs={"data-listing-id": True}))

    # Remove duplicates while preserving order
    seen_ids = set()
    unique_candidates = []
    for el in candidates:
        el_id = id(el)
        if el_id not in seen_ids:
            seen_ids.add(el_id)
            unique_candidates.append(el)

    for card in unique_candidates[:MAX_VEHICLES]:
        v = _parse_card(card, base_url)
        if v:
            vehicles.append(v)

    # Fallback: look for any heading that matches a vehicle title pattern
    if not vehicles:
        vehicles = _extract_from_headings(soup, base_url)

    return vehicles


def _parse_card(card: Any, base_url: str) -> dict[str, Any] | None:
    text = card.get_text(" ", strip=True)
    v: dict[str, Any] = {}

    # VIN from data attributes first
    v["vin"] = card.get("data-vin", "") or card.get("data-vehicle-vin", "") or ""
    if not v["vin"]:
        vin_match = _VIN_RE.search(text)
        if vin_match:
            v["vin"] = vin_match.group(1)

    # Stock number
    v["stock_number"] = str(
        card.get("data-stock-number", "") or card.get("data-stocknumber", "") or ""
    )

    # External ID
    v["external_id"] = str(
        card.get("data-vehicle-id", "")
        or card.get("data-listing-id", "")
        or card.get("data-id", "")
        or ""
    )

    # Title — try heading tags first
    title = ""
    for tag_name in ("h2", "h3", "h4", "h1"):
        heading = card.find(tag_name)
        if heading:
            title = heading.get_text(" ", strip=True)
            break
    if not title:
        # Look for a title class
        title_el = card.find(
            class_=re.compile(
                r"title|name|heading|vehicle-name|car-name", re.IGNORECASE
            )
        )
        if title_el:
            title = title_el.get_text(" ", strip=True)

    if title:
        parsed = _parse_vehicle_title(title)
        v.update(parsed)

    # Year from text if not already found
    if not v.get("year"):
        year_match = _YEAR_RE.search(text)
        if year_match:
            v["year"] = int(year_match.group(1))

    # Price
    price_el = card.find(class_=re.compile(r"price|msrp|cost|amount", re.IGNORECASE))
    price_text = price_el.get_text(" ", strip=True) if price_el else text
    price_match = _PRICE_RE.search(price_text)
    if price_match:
        v["price"] = _parse_int(price_match.group(1))

    # Mileage
    mileage_el = card.find(class_=re.compile(r"mileage|miles|odometer", re.IGNORECASE))
    mileage_text = mileage_el.get_text(" ", strip=True) if mileage_el else text
    mileage_match = _MILEAGE_RE.search(mileage_text)
    if mileage_match:
        v["mileage"] = _parse_int(mileage_match.group(1))

    # Color
    color_el = card.find(
        class_=re.compile(r"ext.?color|exterior.?color|color", re.IGNORECASE)
    )
    if color_el:
        v["ext_color"] = color_el.get_text(" ", strip=True)[:40]

    # Body style
    body_el = card.find(
        class_=re.compile(r"body.?style|body.?type|category", re.IGNORECASE)
    )
    if body_el:
        v["body_style"] = body_el.get_text(" ", strip=True)[:40]

    # Images
    images = []
    for img in card.find_all("img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-original")
            or img.get("data-url")
            or ""
        )
        if not src and img.get("srcset"):
            src = _first_srcset_url(str(img.get("srcset") or ""))
        if not src and img.get("data-srcset"):
            src = _first_srcset_url(str(img.get("data-srcset") or ""))
        if src:
            absolute_src = urllib.parse.urljoin(base_url, str(src))
            if absolute_src.lower().startswith(("http://", "https://")) and not (
                urllib.parse.urlparse(absolute_src).path.lower().endswith((".gif", ".ico"))
            ):
                images.append(absolute_src)
    if images:
        v["images"] = images[:40]

    # Link
    a_tag = card.find("a", href=True)
    if a_tag:
        href = a_tag["href"]
        if href.startswith("/") or href.startswith("http"):
            v["detail_url"] = urllib.parse.urljoin(base_url, href)

    if not _looks_like_vehicle(v):
        return None
    return v


def _extract_from_headings(soup: Any, base_url: str) -> list[dict[str, Any]]:
    """Last-resort: find vehicle titles in any heading on the page."""
    vehicles = []
    for heading in soup.find_all(["h1", "h2", "h3", "h4"]):
        text = heading.get_text(" ", strip=True)
        m = _TITLE_RE.search(text)
        if m:
            v = _parse_vehicle_title(text)
            if v.get("year") and v.get("make"):
                # Try to get price from nearby sibling
                parent = heading.parent
                if parent:
                    price_match = _PRICE_RE.search(parent.get_text(" ", strip=True))
                    if price_match:
                        v["price"] = _parse_int(price_match.group(1))
                    mileage_match = _MILEAGE_RE.search(parent.get_text(" ", strip=True))
                    if mileage_match:
                        v["mileage"] = _parse_int(mileage_match.group(1))
                    a_tag = heading.find("a", href=True) or parent.find("a", href=True)
                    if a_tag:
                        v["detail_url"] = urllib.parse.urljoin(base_url, a_tag["href"])
                vehicles.append(v)
    return vehicles[:MAX_VEHICLES]


# ---------------------------------------------------------------------------
# Strategy 0: Typesense InstantSearch adapter
# ---------------------------------------------------------------------------
# Many OEM dealer websites (Toyota, Honda, Nissan, VW, etc.) built on
# Dealer.com / CDK Roadster / similar platforms use the
# TypesenseInstantSearchAdapter JavaScript library.  The page embeds the
# Typesense API key, host, and collection name in a <script> block, allowing
# us to query the full inventory directly from the Typesense REST API — no
# JS execution required.
#
# Pattern also covers sites using the older Algolia InstantSearch library
# (same detection approach, different query URL).

_TS_ADAPTER_RE = re.compile(
    r"TypesenseInstantSearchAdapter\s*\(\s*\{(.*?)\}\s*\)",
    re.DOTALL,
)
_TS_API_KEY_RE = re.compile(r'apiKey\s*:\s*["\']([^"\']{8,})["\']')
_TS_HOST_RE = re.compile(r'host\s*:\s*["\']([a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,})["\']')
_TS_PORT_RE = re.compile(r"port\s*:\s*(\d+)")
_TS_PROTOCOL_RE = re.compile(r'protocol\s*:\s*["\']([a-z]+)["\']')
_TS_INDEX_RE = re.compile(r'var\s+indexName\s*=\s*["\']([^"\']+)["\']')
_TS_CONDITION_RE = re.compile(r'var\s+srpCondition\s*=\s*["\']([^"\']+)["\']')

_ALGOLIA_RE = re.compile(
    r"algoliasearch\s*\(\s*['\"]([A-Z0-9]{8,12})['\"]\s*,\s*['\"]([a-f0-9]{32})['\"]",
    re.DOTALL,
)
_ALGOLIA_INDEX_RE = re.compile(r'indexName\s*:\s*["\']([^"\']+)["\']')


def _try_typesense(
    html: str, page_url: str, max_vehicles: int
) -> tuple[list[dict[str, Any]], str] | None:
    """Try to extract inventory via the Typesense or Algolia InstantSearch API.

    Returns (vehicles, error_string) if this strategy applies (even if it fails),
    or None if the page doesn't use Typesense/Algolia at all.
    """
    # ---- Typesense detection ----
    adapter_match = _TS_ADAPTER_RE.search(html)
    if adapter_match:
        block = adapter_match.group(0)
        api_key_m = _TS_API_KEY_RE.search(block)
        host_m = _TS_HOST_RE.search(block)
        if not api_key_m or not host_m:
            return [], "Typesense adapter detected but could not extract key/host"
        api_key = api_key_m.group(1)
        host = host_m.group(1)
        port_m = _TS_PORT_RE.search(block)
        port = int(port_m.group(1)) if port_m else 443
        proto_m = _TS_PROTOCOL_RE.search(block)
        protocol = proto_m.group(1) if proto_m else "https"

        # Extract collection name from the surrounding page script
        index_m = _TS_INDEX_RE.search(html)
        if not index_m:
            return [], "Typesense adapter found but could not find indexName variable"
        collection = index_m.group(1)

        # Filter by condition if present
        condition_m = _TS_CONDITION_RE.search(html)
        condition_filter = condition_m.group(1) if condition_m else None

        vehicles, err = _fetch_typesense_all(
            host=host,
            port=port,
            protocol=protocol,
            api_key=api_key,
            collection=collection,
            condition_filter=condition_filter,
            base_url=page_url,
            max_vehicles=max_vehicles,
        )
        return vehicles, err

    # ---- Algolia detection ----
    algolia_m = _ALGOLIA_RE.search(html)
    if algolia_m:
        app_id = algolia_m.group(1)
        api_key = algolia_m.group(2)
        index_m = _ALGOLIA_INDEX_RE.search(html)
        if not index_m:
            return [], "Algolia detected but could not find indexName"
        index = index_m.group(1)
        vehicles, err = _fetch_algolia_all(
            app_id=app_id,
            api_key=api_key,
            index=index,
            base_url=page_url,
            max_vehicles=max_vehicles,
        )
        return vehicles, err

    return None  # Strategy doesn't apply


def _fetch_typesense_all(
    *,
    host: str,
    port: int,
    protocol: str,
    api_key: str,
    collection: str,
    condition_filter: str | None,
    base_url: str,
    max_vehicles: int,
) -> tuple[list[dict[str, Any]], str]:
    """Paginate through all Typesense documents and return normalised vehicles."""
    vehicles: list[dict[str, Any]] = []
    page = 1
    per_page = 100
    base_api = f"{protocol}://{host}:{port}/collections/{collection}/documents/search"
    headers = {"X-TYPESENSE-API-KEY": api_key}

    while len(vehicles) < max_vehicles:
        params: dict[str, Any] = {
            "q": "*",
            "query_by": "make,model,trim,vin,stockNumber",
            "per_page": per_page,
            "page": page,
        }
        if condition_filter:
            params["filter_by"] = f"condition:={condition_filter}"

        try:
            resp = requests.get(
                base_api, headers=headers, params=params, timeout=SCRAPE_TIMEOUT
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            return vehicles, f"Typesense request failed (page {page}): {exc}"
        except (json.JSONDecodeError, ValueError) as exc:
            return vehicles, f"Typesense JSON parse error: {exc}"

        hits = data.get("hits", [])
        if not hits:
            break

        for hit in hits:
            doc = hit.get("document", {})
            v = _map_typesense_doc(doc, base_url)
            if v:
                vehicles.append(v)

        total_found = int(data.get("found", 0))
        if page * per_page >= total_found or len(vehicles) >= max_vehicles:
            break
        page += 1

    return vehicles[:max_vehicles], ""


def _map_typesense_doc(doc: dict[str, Any], base_url: str) -> dict[str, Any] | None:
    """Map a raw Typesense document to our normalised vehicle dict."""
    v: dict[str, Any] = {}

    v["vin"] = str(doc.get("vin", "") or "").strip().upper()
    v["stock_number"] = str(doc.get("stockNumber", "") or "").strip()
    v["external_id"] = str(doc.get("id", "") or "").strip()
    v["year"] = _int_or_none(doc.get("year"))
    v["make"] = str(doc.get("make", "") or "").strip().upper()
    v["model"] = str(doc.get("model", "") or "").strip().upper()
    v["trim"] = str(doc.get("trim", "") or "").strip()
    v["body_style"] = str(
        doc.get("body", "") or doc.get("compoundBody", "") or ""
    ).strip()
    v["ext_color"] = str(doc.get("exteriorColor", "") or "").strip()
    v["int_color"] = str(doc.get("interiorColor", "") or "").strip()
    v["transmission"] = str(
        doc.get("transmission", "") or doc.get("transmissionType", "") or ""
    ).strip()
    v["drivetrain"] = str(doc.get("drivetrain", "") or "").strip()
    v["engine"] = str(doc.get("engine", "") or "").strip()
    v["mileage"] = _int_or_none(doc.get("mileage"))

    # Price: prefer internetPrice → finalPrice → price → msrp
    for price_field in (
        "internetPrice",
        "finalPriceInt",
        "finalPrice",
        "advertisedPrice",
        "price",
        "msrp",
        "sellingPrice",
    ):
        raw = doc.get(price_field)
        if raw:
            p = _int_or_none(str(raw).replace("$", "").replace(",", ""))
            if p and p > 500:
                v["price"] = p
                break

    # Description from features list or vehicleTitle
    features = doc.get("features", [])
    if isinstance(features, list) and features:
        v["description"] = " · ".join(str(f) for f in features[:12])
    elif doc.get("vehicleTitle"):
        v["description"] = str(doc["vehicleTitle"])

    # Images — imageUrls is a list of CDN URLs
    image_urls = doc.get("imageUrls", [])
    if isinstance(image_urls, list):
        v["images"] = [str(u) for u in image_urls if str(u).startswith("http")][:40]
    elif isinstance(image_urls, str) and image_urls.startswith("http"):
        v["images"] = [image_urls]

    # Detail URL
    vdp = str(doc.get("vdpUrl", "") or "").strip()
    if vdp:
        v["detail_url"] = urllib.parse.urljoin(base_url, vdp)

    if not _looks_like_vehicle(v):
        return None
    return v


def _fetch_algolia_all(
    *,
    app_id: str,
    api_key: str,
    index: str,
    base_url: str,
    max_vehicles: int,
) -> tuple[list[dict[str, Any]], str]:
    """Paginate through an Algolia index."""
    vehicles: list[dict[str, Any]] = []
    page = 0
    per_page = 100
    url = f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/{index}/query"
    headers = {
        "X-Algolia-Application-Id": app_id,
        "X-Algolia-API-Key": api_key,
        "Content-Type": "application/json",
    }
    while len(vehicles) < max_vehicles:
        try:
            resp = requests.post(
                url,
                headers=headers,
                json={"query": "", "hitsPerPage": per_page, "page": page},
                timeout=SCRAPE_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            return vehicles, f"Algolia request failed: {exc}"
        except (json.JSONDecodeError, ValueError) as exc:
            return vehicles, f"Algolia JSON error: {exc}"

        hits = data.get("hits", [])
        if not hits:
            break
        for hit in hits:
            v = _map_generic_json_vehicle(hit, base_url)
            if _looks_like_vehicle(v):
                vehicles.append(v)
        nb_pages = int(data.get("nbPages", 1))
        if page + 1 >= nb_pages or len(vehicles) >= max_vehicles:
            break
        page += 1
    return vehicles[:max_vehicles], ""


# ---------------------------------------------------------------------------
# Strategy 4: Common API endpoint discovery
# ---------------------------------------------------------------------------

_API_PATHS = [
    "/api/inventory",
    "/api/vehicles",
    "/api/listings",
    "/inventory.json",
    "/vehicles.json",
    "/api/v1/inventory",
    "/api/v2/inventory",
    "/api/search/inventory",
    "/inventory/search",
    "/ajax/inventory",
    "/feeds/inventory.json",
    "/feeds/vehicles.json",
    # Common DMS / CRM platforms
    "/api/inventory/getInventory",
    "/ws/inventory",
    "/services/inventory",
]


def _try_api_endpoints(base_url: str) -> tuple[list[dict[str, Any]], str]:
    parsed = urllib.parse.urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }
    for path in _API_PATHS:
        url = origin + path
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200 and "json" in (
                resp.headers.get("content-type", "")
            ):
                try:
                    data = resp.json()
                    found = _walk_json_for_vehicles(data, base_url)
                    if found:
                        return found, ""
                except Exception:
                    pass
        except requests.RequestException:
            pass
    return [], ""


# ---------------------------------------------------------------------------
# Strategy 5: AI fallback
# ---------------------------------------------------------------------------

_AI_INVENTORY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vehicles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "year": {"type": ["integer", "null"]},
                    "make": {"type": "string"},
                    "model": {"type": "string"},
                    "trim": {"type": "string"},
                    "vin": {"type": "string"},
                    "stock_number": {"type": "string"},
                    "price": {"type": ["integer", "null"]},
                    "mileage": {"type": ["integer", "null"]},
                    "ext_color": {"type": "string"},
                    "body_style": {"type": "string"},
                    "transmission": {"type": "string"},
                    "drivetrain": {"type": "string"},
                    "detail_url": {"type": "string"},
                    "image_url": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["year", "make", "model", "price"],
                "additionalProperties": False,
            },
        },
        "total_found": {"type": "integer"},
        "has_more_pages": {"type": "boolean"},
    },
    "required": ["vehicles", "total_found"],
    "additionalProperties": False,
}


def _ai_extract(
    html: str,
    url: str,
    openai_api_key: str,
    openai_model: str,
) -> list[dict[str, Any]]:
    """Use OpenAI to extract vehicle listings from HTML."""
    if not BS4_AVAILABLE:
        return []
    # Slim down the HTML — remove scripts/styles, keep text + some structure
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "path", "meta", "link"]):
        tag.decompose()
    clean_html = soup.get_text("\n", strip=True)

    # Limit to first ~12 000 chars to control token cost
    MAX_CHARS = 12_000
    if len(clean_html) > MAX_CHARS:
        clean_html = clean_html[:MAX_CHARS] + "\n[... truncated ...]"

    prompt = (
        "Extract all vehicle listings from the following webpage text. "
        "For each vehicle include: year, make, model, trim, VIN, stock number, "
        "price (integer dollars, no commas), mileage (integer), exterior color, "
        "body style, transmission, drivetrain, a URL to the vehicle detail page if visible, "
        "a primary image URL if visible, and a short description. "
        "Return structured JSON. Page URL: " + url + "\n\n" + clean_html
    )

    try:
        resp = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {openai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": openai_model,
                "input": prompt,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "inventory_extraction",
                        "schema": _AI_INVENTORY_SCHEMA,
                        "strict": True,
                    }
                },
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        output_text = (
            data.get("output", [{}])[0].get("content", [{}])[0].get("text", "{}")
        )
        result = json.loads(output_text)
        raw_vehicles = result.get("vehicles", [])
        vehicles = []
        for rv in raw_vehicles:
            v: dict[str, Any] = {}
            v["year"] = _int_or_none(rv.get("year"))
            v["make"] = str(rv.get("make", "") or "")
            v["model"] = str(rv.get("model", "") or "")
            v["trim"] = str(rv.get("trim", "") or "")
            v["vin"] = str(rv.get("vin", "") or "")
            v["stock_number"] = str(rv.get("stock_number", "") or "")
            v["price"] = _int_or_none(rv.get("price"))
            v["mileage"] = _int_or_none(rv.get("mileage"))
            v["ext_color"] = str(rv.get("ext_color", "") or "")
            v["body_style"] = str(rv.get("body_style", "") or "")
            v["transmission"] = str(rv.get("transmission", "") or "")
            v["drivetrain"] = str(rv.get("drivetrain", "") or "")
            v["detail_url"] = str(rv.get("detail_url", "") or "")
            img = str(rv.get("image_url", "") or "")
            if img:
                v["images"] = [img]
            v["description"] = str(rv.get("description", "") or "")
            if _looks_like_vehicle(v):
                vehicles.append(v)
        return vehicles
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Pagination detection
# ---------------------------------------------------------------------------


def _find_next_page(html: str, current_url: str) -> str | None:
    if not BS4_AVAILABLE:
        return None
    soup = BeautifulSoup(html, "html.parser")

    # Look for rel="next" link
    rel_next = soup.find("link", rel="next")
    if rel_next and rel_next.get("href"):
        return urllib.parse.urljoin(current_url, rel_next["href"])

    # Look for "next" button / link
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(strip=True).lower()
        aria = str(anchor.get("aria-label", "")).lower()
        cls = " ".join(anchor.get("class", [])).lower()
        rel = str(anchor.get("rel", "")).lower()
        if any(
            keyword in val
            for val in (text, aria, cls, rel)
            for keyword in ("next", "›", "»", "next page")
        ):
            href = anchor["href"]
            if href and href not in ("#", "javascript:void(0)", "javascript:;"):
                return urllib.parse.urljoin(current_url, href)

    return None


# ---------------------------------------------------------------------------
# Generic JSON vehicle field mapping
# ---------------------------------------------------------------------------

# Maps common field names from arbitrary JSON to our normalised fields
_FIELD_MAP: dict[str, str] = {
    "vin": "vin",
    "vinnumber": "vin",
    "vinno": "vin",
    "vehicleidentificationnumber": "vin",
    "year": "year",
    "modelyear": "year",
    "yearmodel": "year",
    "make": "make",
    "makename": "make",
    "manufacturer": "make",
    "model": "model",
    "modelname": "model",
    "trim": "trim",
    "trimname": "trim",
    "trimlevel": "trim",
    "trim_level": "trim",
    "series": "trim",
    "bodystyle": "body_style",
    "body_style": "body_style",
    "body": "body_style",
    "bodytype": "body_style",
    "body_type": "body_style",
    "vehicletype": "body_style",
    "style": "body_style",
    "price": "price",
    "askingprice": "price",
    "dealerprice": "price",
    "displayprice": "price",
    "finalprice": "price",
    "saleprice": "price",
    "sellingprice": "price",
    "internetprice": "price",
    "listprice": "price",
    "ourprice": "price",
    "primaryprice": "price",
    "retailprice": "price",
    "msrp": "price",
    "mileage": "mileage",
    "odometer": "mileage",
    "odometerreading": "mileage",
    "odometervalue": "mileage",
    "miles": "mileage",
    "exteriorcolor": "ext_color",
    "exterior_color": "ext_color",
    "exterior": "ext_color",
    "exteriorcolorname": "ext_color",
    "extcolor": "ext_color",
    "color": "ext_color",
    "interiorcolor": "int_color",
    "interior_color": "int_color",
    "interior": "int_color",
    "interiorcolorname": "int_color",
    "intcolor": "int_color",
    "stocknumber": "stock_number",
    "stock_number": "stock_number",
    "stockno": "stock_number",
    "stocknum": "stock_number",
    "stock": "stock_number",
    "id": "external_id",
    "vehicleid": "external_id",
    "vehicle_id": "external_id",
    "listingid": "external_id",
    "listing_id": "external_id",
    "objectid": "external_id",
    "transmission": "transmission",
    "drivetrain": "drivetrain",
    "drivetype": "drivetrain",
    "drive": "drivetrain",
    "engine": "engine",
    "enginedescription": "engine",
    "description": "description",
    "comments": "description",
    "url": "detail_url",
    "detailurl": "detail_url",
    "permalink": "detail_url",
    "vehicleurl": "detail_url",
    "vdpurl": "detail_url",
    "link": "detail_url",
    "href": "detail_url",
    "image": "images",
    "imageurl": "images",
    "mainphoto": "images",
    "photo": "images",
    "photourl": "images",
    "photourls": "images",
    "photos": "images",
    "thumbnail": "images",
    "thumbnailurl": "images",
    "images": "images",
    "imageurls": "images",
}


def _map_generic_json_vehicle(data: dict[str, Any], base_url: str) -> dict[str, Any]:
    v: dict[str, Any] = {}
    for key, val in data.items():
        mapped = _FIELD_MAP.get(key.lower().replace("-", "").replace("_", ""))
        if mapped:
            if mapped == "images":
                images = _coerce_image_urls(val, base_url)
                if images:
                    v["images"] = images[:40]
            elif mapped in ("year", "price", "mileage"):
                v[mapped] = _int_or_none(val)
            else:
                v[mapped] = str(val or "").strip()

    # Try to resolve relative detail_url
    if v.get("detail_url") and not v["detail_url"].startswith("http"):
        v["detail_url"] = urllib.parse.urljoin(base_url, v["detail_url"])

    # Check for title field
    for title_key in ("title", "name", "vehiclename", "vehicle_name", "listingtitle"):
        if title_key in {k.lower() for k in data}:
            raw_title = next(
                v_raw for k, v_raw in data.items() if k.lower() == title_key
            )
            parsed = _parse_vehicle_title(str(raw_title or ""))
            for field in ("year", "make", "model", "trim"):
                if not v.get(field) and parsed.get(field):
                    v[field] = parsed[field]
            break

    return v


def _coerce_image_urls(value: Any, base_url: str, depth: int = 0) -> list[str]:
    if depth > 4:
        return []

    if isinstance(value, str):
        url = value.strip()
        if not url:
            return []
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith(("/", "./", "../")):
            url = urllib.parse.urljoin(base_url, url)
        if url.lower().startswith(("http://", "https://")):
            return [url]
        return []

    if isinstance(value, list):
        images: list[str] = []
        for item in value[:80]:
            images.extend(_coerce_image_urls(item, base_url, depth + 1))
        return images

    if isinstance(value, dict):
        images = []
        for key, nested in value.items():
            normalized = key.lower().replace("-", "").replace("_", "")
            if normalized in {
                "href",
                "image",
                "imageurl",
                "large",
                "main",
                "mainphoto",
                "photo",
                "photourl",
                "src",
                "thumbnail",
                "thumbnailurl",
                "url",
            }:
                images.extend(_coerce_image_urls(nested, base_url, depth + 1))
        return images

    return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _looks_like_vehicle(v: dict[str, Any]) -> bool:
    """Return True if *v* has enough fields to be considered a real vehicle."""
    has_year = bool(v.get("year"))
    has_make = bool(str(v.get("make", "") or "").strip())
    has_model = bool(str(v.get("model", "") or "").strip())
    has_vin = bool(str(v.get("vin", "") or "").strip())
    has_price = v.get("price") is not None
    has_stock = bool(v.get("stock_number"))
    return (
        (has_year and has_make and has_model)
        or (has_vin and (has_year or has_make))
        or (has_make and has_model and has_price)
        or (has_vin and has_price)
        or (has_stock and (has_make or has_year))
    )


def _vehicle_key(v: dict[str, Any]) -> str:
    vin = str(v.get("vin", "") or "").upper().strip()
    if vin and len(vin) == 17:
        return f"vin:{vin}"
    stock = str(v.get("stock_number", "") or "").strip()
    if stock:
        return f"stock:{stock}"
    ext_id = str(v.get("external_id", "") or "").strip()
    if ext_id:
        return f"ext:{ext_id}"
    year = str(v.get("year", "") or "")
    make = str(v.get("make", "") or "").upper()
    model = str(v.get("model", "") or "").upper()
    price = str(v.get("price", "") or "")
    return f"{year}:{make}:{model}:{price}"


_MAKE_RE = re.compile(
    r"\b(19[6-9]\d|20[0-4]\d)\s+"
    r"(Acura|Alfa Romeo|Aston Martin|Audi|Bentley|BMW|Buick|Cadillac|Chevrolet|Chevy|"
    r"Chrysler|Dodge|Ferrari|Fiat|Ford|Genesis|GMC|Honda|Hyundai|Infiniti|Jaguar|Jeep|"
    r"Kia|Lamborghini|Land Rover|Lexus|Lincoln|Lotus|Lucid|Maserati|Mazda|McLaren|"
    r"Mercedes-Benz|Mercedes|MINI|Mitsubishi|Nissan|Polestar|Porsche|Ram|Rivian|Rolls-Royce|"
    r"Subaru|Tesla|Toyota|Volkswagen|VW|Volvo|[A-Z][a-z]+)"
    r"\s+([A-Z0-9][a-zA-Z0-9\-/& ]+?)(?:\s{2,}|\Z|,|\|)",
    re.IGNORECASE,
)


def _parse_vehicle_title(title: str) -> dict[str, Any]:
    title = title.strip()
    v: dict[str, Any] = {}
    m = _MAKE_RE.search(title)
    if m:
        v["year"] = int(m.group(1))
        v["make"] = m.group(2).strip().title()
        rest = m.group(3).strip()
        parts = rest.split()
        v["model"] = parts[0] if parts else ""
        v["trim"] = " ".join(parts[1:]) if len(parts) > 1 else ""
        return v

    # Generic fallback: "YEAR WORDS..."
    parts = title.split()
    if parts and _YEAR_RE.match(parts[0]):
        v["year"] = int(parts[0])
        if len(parts) > 1:
            v["make"] = parts[1].title()
        if len(parts) > 2:
            v["model"] = parts[2]
        if len(parts) > 3:
            v["trim"] = " ".join(parts[3:])
    return v


def _int_or_none(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(str(val).replace(",", "").replace("$", "").strip().split(".")[0])
    except (ValueError, AttributeError):
        return None


def _parse_int(val: str) -> int | None:
    return _int_or_none(val.replace(",", ""))


def _first_srcset_url(srcset: str) -> str:
    for candidate in srcset.split(","):
        url = candidate.strip().split(" ", 1)[0]
        if url:
            return url
    return ""


def _rank_vehicle_images(images: list[Any], *, limit: int = 12) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for raw in images:
        url = str(raw or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        key = url.split("#", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        unique.append(url)

    def image_score(item: tuple[int, str]) -> tuple[int, int]:
        index, url = item
        lower = url.lower()
        path = urllib.parse.urlparse(url).path.lower()
        is_bad = any(marker in lower for marker in _BAD_IMAGE_MARKERS)
        if is_bad:
            return -1000, -index
        score = 0
        if any(marker in lower for marker in _REAL_IMAGE_MARKERS):
            score += 80
        if path.endswith((".jpg", ".jpeg")):
            score += 16
        elif path.endswith(".webp"):
            score += 12
        elif path.endswith(".png"):
            score += 6
        if "front" in lower or "angle" in lower or "hero" in lower:
            score += 8
        if "side" in lower:
            score += 3
        if "interior" in lower:
            score -= 2
        if "back" in lower or "rear" in lower:
            score -= 4
        if any(marker in lower for marker in _RENDER_IMAGE_MARKERS):
            score -= 24
        return score, -index

    ranked = sorted(enumerate(unique), key=image_score, reverse=True)
    return [url for _index, url in ranked[:limit]]


def _normalise(v: dict[str, Any]) -> dict[str, Any]:
    """Return a clean, consistent vehicle dict."""
    return {
        "external_id": str(v.get("external_id", "") or "")[:120],
        "vin": str(v.get("vin", "") or "").upper().strip()[:17],
        "stock_number": str(v.get("stock_number", "") or "")[:40],
        "year": _int_or_none(v.get("year")),
        "make": str(v.get("make", "") or "").strip().upper()[:60],
        "model": str(v.get("model", "") or "").strip().upper()[:80],
        "trim": str(v.get("trim", "") or "").strip()[:80],
        "body_style": str(v.get("body_style", "") or "").strip()[:60],
        "price": _int_or_none(v.get("price")),
        "mileage": _int_or_none(v.get("mileage")),
        "ext_color": str(v.get("ext_color", "") or "").strip()[:60],
        "int_color": str(v.get("int_color", "") or "").strip()[:60],
        "transmission": str(v.get("transmission", "") or "").strip()[:60],
        "drivetrain": str(v.get("drivetrain", "") or "").strip()[:60],
        "engine": str(v.get("engine", "") or "").strip()[:120],
        "description": str(v.get("description", "") or "").strip()[:2000],
        "images": _rank_vehicle_images(
            v.get("images", []) if isinstance(v.get("images"), list) else []
        ),
        "detail_url": str(v.get("detail_url", "") or "").strip()[:500],
    }
