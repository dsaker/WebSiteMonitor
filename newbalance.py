import os
import json
import sys
import time
import html
import re
import traceback
import requests
from curl_cffi import requests as cffi_requests
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
TARGET_URL = "https://www.newbalance.com/men/shoes/lifestyle/?ICID=PGP_X_PGP_574_Lifestyle_NB4905_M&refinementList%5Bcustom.seriesNumber%5D%5B0%5D=574"
HOME_URL = "https://www.newbalance.com/"
SCAPI_SEARCH_URL = "https://6pt47ivs.api.commercecloud.salesforce.com/search/shopper-search/v1/organizations/f_ecom_aagi_prd/product-search"
SERIES_NAME = "New Balance 574"
SEEN_SHOES_FILE = "seen_shoes.json"

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip().strip('"\'') or None

# Standard headers mimicking modern browser requests
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1"
}


class ShoeAgentError(Exception):
    """Raised when critical retrieval or execution steps fail."""


# ==========================================
# STATE MANAGEMENT
# ==========================================

def load_seen_shoes() -> set:
    """Load previously notified shoe IDs from the local JSON state file."""
    if os.path.exists(SEEN_SHOES_FILE):
        try:
            with open(SEEN_SHOES_FILE, "r") as f:
                data = json.load(f)
                return set(data) if isinstance(data, list) else set()
        except json.JSONDecodeError:
            return set()
    return set()


def save_seen_shoes(seen_ids: set):
    """Persist updated shoe IDs back to disk."""
    with open(SEEN_SHOES_FILE, "w") as f:
        json.dump(sorted(list(seen_ids)), f, indent=2)


# ==========================================
# FETCHING & PARSING
# ==========================================

def fetch_shoes(url: str = TARGET_URL) -> list:
    """
    Fetches the New Balance 574 listings via Commerce Cloud API using browser TLS impersonation.
    Falls back to HTML parsing if needed.
    """
    max_retries = 3
    base_delay = 3
    timeout = 30

    for attempt in range(1, max_retries + 1):
        try:
            session = cffi_requests.Session(impersonate="chrome124")
            home_res = session.get(HOME_URL, timeout=timeout)
            home_res.raise_for_status()

            token = session.cookies.get("cc-at_NBUS")
            if token:
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }
                params = {
                    "siteId": "NBUS",
                    "refine": ["c_seriesNumber=574"],
                    "limit": 50,
                }
                api_res = session.get(SCAPI_SEARCH_URL, headers=headers, params=params, timeout=timeout)
                api_res.raise_for_status()
                data = api_res.json()
                shoes = parse_shoes_from_api(data)
                if shoes:
                    print(f"[*] Extracted {len(shoes)} total shoe styles from listing.")
                    return shoes

            # Fallback to direct page fetch if token/API is unavailable
            res = session.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
            res.raise_for_status()
            shoes = parse_shoes_from_html(res.text)
            return shoes
        except Exception as e:
            if attempt < max_retries:
                delay = base_delay * attempt
                print(f"[!] Fetch attempt {attempt} failed: {e}. Retrying in {delay}s...")
                time.sleep(delay)
            else:
                raise ShoeAgentError(f"Failed to fetch New Balance listings after {max_retries} attempts: {e}") from e


def parse_shoes_from_api(data: dict) -> list:
    """
    Parses product hits and style colorways from the Commerce Cloud search API response.
    """
    shoes = []
    hits = data.get("hits", [])

    for hit in hits:
        pid = hit.get("productId")
        pname = hit.get("productName", "574")
        price_val = hit.get("price")
        price = f"${price_val:.2f}" if isinstance(price_val, (int, float)) else f"${price_val or 'N/A'}"
        variation_attrs = hit.get("variationAttributes") or []
        style_attr = next((va for va in variation_attrs if va.get("id") == "style"), None)

        if style_attr and style_attr.get("values"):
            for val in style_attr["values"]:
                style_code = val.get("value")
                style_name = val.get("name") or style_code
                shoes.append({
                    "id": style_code,
                    "title": f"New Balance {pname} ({style_code})",
                    "price": price,
                    "color": style_name,
                    "url": f"https://www.newbalance.com/pd/{pid}.html?dwvar_{pid}_style={style_code}",
                    "image_url": f"https://nb.scene7.com/is/image/NB/{style_code.lower()}_nb_02_i?wid=800&hei=800",
                })
        else:
            rep_id = hit.get("representedProduct", {}).get("id", pid)
            style_code = rep_id.split("-")[0] if "-" in rep_id else pid
            shoes.append({
                "id": str(pid),
                "title": f"New Balance {pname}",
                "price": price,
                "color": "Standard",
                "url": f"https://www.newbalance.com/pd/{pid}.html",
                "image_url": f"https://nb.scene7.com/is/image/NB/{style_code.lower()}_nb_02_i?wid=800&hei=800",
            })

    return shoes


def parse_shoes_from_html(html_text: str) -> list:
    """
    Parses product tiles and embedded structured data (JSON-LD or product grids).
    Extracts ID, title, colorway, price, product URL, and thumbnail image.
    """
    shoes = []

    # Strategy 1: Check for embedded JSON-LD schema
    json_ld_matches = re.findall(r'<script type="application/ld\+json">({.*?})</script>', html_text, re.DOTALL)
    for block in json_ld_matches:
        try:
            data = json.loads(block)
            items = data.get("itemListElement", []) if isinstance(data, dict) else []
            for item in items:
                prod = item.get("item", {})
                pid = prod.get("sku") or prod.get("productID") or prod.get("url", "")
                if pid:
                    shoes.append({
                        "id": str(pid),
                        "title": prod.get("name", "New Balance 574"),
                        "price": f"${prod.get('offers', {}).get('price', 'N/A')}",
                        "color": prod.get("color", "Standard"),
                        "url": prod.get("url", TARGET_URL),
                        "image_url": prod.get("image", ""),
                    })
        except Exception:
            continue

    # Strategy 2: Extract via HTML tile patterns if JSON-LD is absent or partial
    if not shoes:
        # Regex extraction for common e-commerce product markup attributes
        tile_pattern = re.compile(
            r'data-pid=["\'](?P<pid>[^"\']+)["\'].*?'
            r'(?:data-product-name=["\'](?P<name>[^"\']+)["\'])?.*?'
            r'(?:data-price=["\'](?P<price>[^"\']+)["\'])?',
            re.DOTALL
        )
        for match in tile_pattern.finditer(html_text):
            pid = match.group("pid")
            name = match.group("name") or "New Balance 574"
            price = match.group("price") or "Check Website"
            shoes.append({
                "id": str(pid),
                "title": html.unescape(name),
                "price": price if price.startswith("$") else f"${price}",
                "color": "New Balance Colorway",
                "url": f"https://www.newbalance.com/pd/{pid}.html",
                "image_url": "",
            })

    print(f"[*] Extracted {len(shoes)} total shoe styles from listing.")
    return shoes


# ==========================================
# DISCORD NOTIFICATIONS
# ==========================================

def send_discord_shoe_notification(shoe: dict):
    """Sends a rich Discord embed for an individual newly discovered shoe."""
    if not DISCORD_WEBHOOK_URL:
        print(
            f"[!] DISCORD_WEBHOOK_URL not configured. Discovered:\n  -> {shoe['title']} ({shoe['price']}) - {shoe['url']}")
        return

    embed = {
        "title": f"👟 New Release: {shoe['title']}",
        "url": shoe["url"],
        "color": 15158332,  # New Balance red tone (Hex: #E71D34)
        "fields": [
            {"name": "Series", "value": SERIES_NAME, "inline": True},
            {"name": "Price", "value": shoe.get("price", "N/A"), "inline": True},
            {"name": "Style / SKU", "value": shoe.get("id", "Unknown"), "inline": True},
            {"name": "Colorway", "value": shoe.get("color", "Unspecified"), "inline": False},
            {"name": "🔗 Product Link", "value": f"[View on NewBalance.com]({shoe['url']})", "inline": False}
        ],
        "footer": {"text": "New Balance 574 Monitor • Automated Tracker"}
    }

    if shoe.get("image_url"):
        embed["thumbnail"] = {"url": shoe["image_url"]}

    payload = {
        "content": "🚨 **New '574' Colorway / Shoe Detected!**",
        "embeds": [embed]
    }

    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        res.raise_for_status()
        print(f"[+] Discord alert sent for: {shoe['title']} ({shoe['id']})")
    except Exception as e:
        raise ShoeAgentError(f"Failed to send Discord alert for '{shoe['title']}': {e}") from e


def send_discord_summary(total_scanned: int, new_found: int, new_shoes: list = None):
    """Sends a summary notification after completing the run."""
    if not DISCORD_WEBHOOK_URL:
        print(f"[!] Run summary: Scanned: {total_scanned}, New: {new_found}")
        return

    fields = [
        {"name": "Total Active Listings", "value": str(total_scanned), "inline": True},
        {"name": "New Releases Detected", "value": str(new_found), "inline": True},
    ]

    if new_shoes:
        item_lines = [f"• [{shoe['title']} ({shoe['price']})]({shoe['url']})" for shoe in new_shoes[:10]]
        fields.append({
            "name": "✨ New Arrivals",
            "value": "\n".join(item_lines)[:1024],
            "inline": False
        })
    else:
        fields.append({
            "name": "Status",
            "value": "No new 574 colorways released since last check.",
            "inline": False
        })

    embed = {
        "title": "📊 New Balance 574 Daily Check Complete",
        "color": 3066993 if new_found > 0 else 5814783,
        "fields": fields,
        "footer": {"text": "New Balance Monitor Agent"}
    }

    payload = {
        "content": "📋 **New Balance Daily Scan Complete**",
        "embeds": [embed]
    }

    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        res.raise_for_status()
        print("[+] Discord summary sent.")
    except Exception as e:
        print(f"[-] Could not send Discord summary: {e}")


def send_discord_error(message: str, details: str = ""):
    """Reports execution failure to Discord."""
    print(f"[-] FATAL: {message}")
    if not DISCORD_WEBHOOK_URL:
        return

    fields = [{"name": "Error", "value": message[:1024], "inline": False}]
    if details:
        fields.append({"name": "Details", "value": f"```\n{details[:1000]}\n```", "inline": False})

    embed = {
        "title": "❌ New Balance Monitor Run Failed",
        "color": 15158332,
        "fields": fields,
        "footer": {"text": "New Balance Monitor Agent • Run Aborted"}
    }

    try:
        requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": "🛑 **New Balance Agent encountered an error.**", "embeds": [embed]},
            timeout=10
        )
    except Exception as e:
        print(f"[-] Could not send Discord error notice: {e}")


# ==========================================
# MAIN EXECUTION
# ==========================================

def main(seen_shoes: set):
    shoes = fetch_shoes(TARGET_URL)
    new_found = 0
    new_shoes = []

    for shoe in shoes:
        shoe_id = shoe["id"]
        if shoe_id in seen_shoes:
            continue

        print(f"[*] New shoe detected: {shoe['title']} ({shoe_id})")
        send_discord_shoe_notification(shoe)

        seen_shoes.add(shoe_id)
        save_seen_shoes(seen_shoes)
        new_shoes.append(shoe)
        new_found += 1

    print(f"[✓] Check complete. {new_found} new shoe(s) processed.")
    send_discord_summary(len(shoes), new_found, new_shoes)


if __name__ == "__main__":
    seen = load_seen_shoes()
    try:
        main(seen)
    except Exception as exc:
        save_seen_shoes(seen)
        send_discord_error(
            message=str(exc) if isinstance(exc, ShoeAgentError) else f"{type(exc).__name__}: {exc}",
            details=traceback.format_exc()
        )
        sys.exit(1)
    save_seen_shoes(seen)