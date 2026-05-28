"""
Filename: scraper_subito.py
Version: 4.0.0
Date: 2026-05-28
Author: Leonardo Lisa
Description: Mobile API Scraper for Subito.it. Implements DataDome WAF evasion 
             via curl_cffi TLS impersonation, Panther hardware fingerprinting, 
             and dynamic UUID state management. Replaces the legacy Web/HTML scraper.
Requirements: curl_cffi beautifulsoup4 pillow

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import json
import time
import random
import os
import uuid
from urllib.parse import urlparse, parse_qsl
from curl_cffi import requests as cffi_requests
from PIL import Image
from io import BytesIO

ENV_FILE = ".subito_env.json"

def get_or_create_environment_id() -> str:
    """
    Retrieves the persistent environment UUID from disk or generates a new one.
    Simulates the initial application installation state to bypass WAF anti-rotation rules.
    """
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, 'r') as f:
            try:
                data = json.load(f)
                return data.get("environment_id", str(uuid.uuid4()))
            except json.JSONDecodeError:
                pass
    
    new_env_id = str(uuid.uuid4())
    with open(ENV_FILE, 'w') as f:
        json.dump({"environment_id": new_env_id}, f)
    return new_env_id

def generate_session_id() -> str:
    """
    Generates a volatile session UUID. Simulates an application cold start.
    """
    return str(uuid.uuid4())

class SubitoScraper:
    def __init__(self):
        self.api_url = "https://hades.subito.it/v1/search/items"
        self.debug_mode = False
        self.env_id = get_or_create_environment_id()
        self._reset_session()

    def _reset_session(self):
        """Initializes or resets the HTTP session to clear tainted cookies and rotate the session UUID."""
        self.session_id = generate_session_id()
        self.session = cffi_requests.Session(impersonate="chrome110")
        
        self.session.headers.update({
            "user-agent": "Subito/101700000 (Linux; U; Android 13; panther Build/TQ3A.230805.001)",
            "x-subito-channel": "android",
            "x-subito-fingerprint": "channel-id=4;client-version=101700000;agent-version=33;device-id=Pixel 7;device-version=13;network=WIFI",
            "x-subito-session-id": self.session_id,
            "x-subito-version": "10.17.0",
            "x-subito-environment-id": self.env_id,
            "accept-encoding": "gzip",
            "accept": "application/json"
        })
        
        if self.debug_mode:
            print(f"\033[96m[API DEBUG] Session reset. Env: {self.env_id[:8]} | Sess: {self.session_id[:8]}\033[0m")

    def _debug_print(self, error_msg):
        if self.debug_mode:
            print(f"\033[93m[SCRAPER ERROR] {error_msg}\033[0m")
            
    def _parse_web_url_to_api(self, web_url: str) -> dict:
        """
        Translates a legacy Web URL added by users into Hades API query parameters.
        Maintains backwards compatibility with the existing telegram_ui commands.
        """
        params = {
            "sort": "datedesc", 
            "t": "s",           # Sale items
            "shp": "true",      # Shipping flag
            "lim": "20",
            "pin": "0,0,0"
        }
        
        parsed = urlparse(web_url)
        qs = dict(parse_qsl(parsed.query))
        
        if 'q' in qs:
            params['q'] = qs['q']
            
        # Map path fragments to primary category IDs
        path = parsed.path
        if '/informatica/' in path: params['c'] = '10'
        elif '/console-videogiochi/' in path: params['c'] = '73'
        elif '/telefonia/' in path: params['c'] = '15'
        elif '/auto/' in path: params['c'] = '2'
        elif '/moto-e-scooter/' in path: params['c'] = '3'
        
        return params

    def fetch_ads(self, url: str) -> list:
        """
        Fetches ads using the Mobile API instead of HTML parsing.
        Implements randomized exponential backoff for WAF 429/403 blocks.
        """
        api_params = self._parse_web_url_to_api(url)
        retries = 3
        base_wait_time = 5
        
        for attempt in range(retries):
            try:
                res = self.session.get(self.api_url, params=api_params, timeout=15)
                
                if res.status_code == 200:
                    return self._parse_api_response(res.json())
                
                if res.status_code == 403:
                    wait_reset = random.uniform(8.0, 12.0)
                    self._debug_print(f"WAF Block (403). Rotating Session UUID in {wait_reset:.2f}s...")
                    time.sleep(wait_reset)
                    self._reset_session()
                    continue

                if res.status_code == 429:
                    jittered_wait = base_wait_time * random.uniform(0.8, 1.2)
                    self._debug_print(f"Rate Limit (429). Retrying in {jittered_wait:.2f}s...")
                    time.sleep(jittered_wait)
                    base_wait_time *= 2
                    continue
                
                self._debug_print(f"HTTP {res.status_code} for API Request")
                return []
                
            except Exception as e:
                self._debug_print(f"fetch_ads network exception: {e}")
                if attempt < retries - 1:
                    time.sleep(base_wait_time * random.uniform(0.8, 1.2))
                    base_wait_time *= 2
                    continue
                return []
        return []

    def _parse_api_response(self, json_data: dict) -> list:
        """
        Maps the Hades API JSON structure to the bot's standard dictionary format.
        """
        parsed_ads = []
        items_list = json_data.get('ads', [])
        
        for ad in items_list:
            try:
                # Discard sold items 
                if ad.get('type', {}).get('key') == 'sold':
                    continue
                    
                # Parse standard URN string (e.g., id:ad:UUID:list:ID)
                urn = ad.get('urn', '')
                ad_id = urn.split(':')[-1] if ':' in urn else urn
                
                title = ad.get('subject', 'No Title')
                desc = ad.get('body', 'No description.')
                
                # Isolate pricing value
                price = "Unknown price"
                features = ad.get('features', [])
                for f in features:
                    if f.get('uri') == '/price':
                        vals = f.get('values', [])
                        if vals: price = vals[0].get('value', price)
                        break
                        
                # Extract hierarchical geolocation
                geo = ad.get('geo', {})
                town = geo.get('town', {}).get('value', 'Unknown')
                city = geo.get('city', {}).get('short_name', '?')
                location = f"{town} ({city})"
                
                # Construct absolute routing URL
                link = ad.get('urls', {}).get('default', '')
                if not link.startswith('http'):
                    link = f"https://www.subito.it{link}"
                
                images = ad.get('images', [])
                primary_image = images[0] if images else {}
                image_url = self._extract_image_url(primary_image)

                parsed_ads.append({
                    "id": ad_id,
                    "title": title,
                    "price": price,
                    "location": location,
                    "description": desc,
                    "link": link,
                    "image_url": image_url
                })
            except Exception as e:
                self._debug_print(f"Error parsing ad node: {e}")
                
        return parsed_ads

    def _extract_image_url(self, image_node: dict) -> str:
        """
        Parses the image node and converts internal URNs to valid CDN URLs via sharding logic.
        """
        uri = image_node.get("secureuri") or image_node.get("uri")
        if not uri: return None

        if uri.startswith("imgid:"):
            img_id = uri.replace("imgid:", "")
            shard = img_id[:2]
            return f"https://s.sbito.it/images/{shard}/{img_id}.jpg"
            
        return uri

    def download_image(self, image_url: str) -> bytes:
        """
        Downloads the image utilizing the active session to maintain WAF trust state.
        Strictly forces JPEG encoding in RAM before broadcasting.
        """
        if not image_url: return None
        dl_headers = {"Accept": "image/jpeg,image/webp,image/apng,image/*,*/*;q=0.8"}
        
        try:
            res = self.session.get(image_url, headers=dl_headers, timeout=15)
            if res.status_code != 200: 
                self._debug_print(f"Image CDN HTTP {res.status_code}: {image_url}")
                return None
                
            img = Image.open(BytesIO(res.content))
            if img.mode != 'RGB':
                img = img.convert('RGB')
                
            out_io = BytesIO()
            img.save(out_io, format='JPEG', quality=90)
            return out_io.getvalue()
        except Exception as e:
            self._debug_print(f"download_image exception: {e}")
            return None