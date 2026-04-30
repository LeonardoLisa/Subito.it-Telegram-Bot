"""
Filename: scraper_subito.py
Version: 3.3.0
Date: 2026-04-29
Author: Leonardo Lisa
Description: Target-specific Web Scraper for Subito.it. Implements WAF evasion, randomized exponential backoff retries, and image extraction.
Requirements: curl_cffi, beautifulsoup4, pillow

GNU GPLv3 Prelude:
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
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup
from PIL import Image
from io import BytesIO

class SubitoScraper:
    def __init__(self):
        self.base_url = "https://www.subito.it/"
        self.debug_mode = False
        self._reset_session()

    def _reset_session(self):
        """Initializes or resets the HTTP session to clear tainted cookies."""
        self.session = cffi_requests.Session(impersonate="safari15_3")
        self.session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Referer": "https://www.google.com/"
        })

    def _debug_print(self, error_msg):
        if self.debug_mode:
            print(f"\033[93m[SCRAPER ERROR] {error_msg}\033[0m")
        
    def fetch_ads(self, url):
        """
        Fetches the target URL with session reset on 403 errors and randomized exponential backoff.
        """
        retries = 3
        base_wait_time = 5  # Initial wait base in seconds
        
        for attempt in range(retries):
            try:
                res = self.session.get(url, timeout=15)
                
                # Success: Parse and return results
                if res.status_code == 200:
                    return self._parse_response(res.text)
                
                # Blocked: Wait ~10s, reset session, wait ~3s
                if res.status_code == 403:
                    wait_reset = random.uniform(8.0, 12.0)
                    wait_after = random.uniform(2.0, 4.0)
                    
                    self._debug_print(f"Attempt {attempt + 1} failed (HTTP 403). Waiting {wait_reset:.2f}s to reset session...")
                    time.sleep(wait_reset)
                    self._reset_session()
                    self._debug_print(f"Session reset. Waiting {wait_after:.2f}s before retrying...")
                    time.sleep(wait_after)
                    continue

                # Rate limited: Retry with randomized exponential backoff
                if res.status_code == 429:
                    jittered_wait = base_wait_time * random.uniform(0.8, 1.2)
                    self._debug_print(f"Attempt {attempt + 1} failed (HTTP 429). Retrying in {jittered_wait:.2f}s...")
                    time.sleep(jittered_wait)
                    base_wait_time *= 2
                    continue
                
                self._debug_print(f"HTTP {res.status_code} for URL: {url}")
                return []
                
            except Exception as e:
                self._debug_print(f"fetch_ads exception: {e}")
                if attempt < retries - 1:
                    jittered_wait = base_wait_time * random.uniform(0.8, 1.2)
                    time.sleep(jittered_wait)
                    base_wait_time *= 2
                    continue
                return []
        return []

    def _parse_response(self, html_content):
        """
        Internal helper to parse the __NEXT_DATA__ JSON payload from Subito.it.
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        script_tag = soup.find('script', id='__NEXT_DATA__')
        if not script_tag:
            self._debug_print("__NEXT_DATA__ JSON payload not found in HTML.")
            return []
            
        try:
            json_data = json.loads(script_tag.string)
            # Navigate the JSON schema to find the items list
            items_node = json_data.get('props', {}).get('pageProps', {}).get('initialState', {}).get('items', {})
            items_list = items_node.get('originalList', [])
            
            parsed_ads = []
            for product in items_list:
                ad_data = product.get('item', product)
                link = ad_data.get('urls', {}).get('default', '')
                
                # Filter out sold items or missing links
                if not link or ad_data.get('sold', False):
                    continue
                    
                location_geo = ad_data.get('geo', {})
                location = f"{location_geo.get('town', {}).get('value', 'Unknown')} ({location_geo.get('city', {}).get('shortName', '?')})"
                
                price = "Unknown price"
                features = ad_data.get('features', {})
                price_feature = features.get('/price')
                if price_feature and 'values' in price_feature:
                    price = price_feature['values'][0].get('key', price)
                    
                parsed_ads.append({
                    "id": ad_data.get('urn', '').split(':')[-1],
                    "title": ad_data.get('subject', 'No Title'),
                    "price": price,
                    "location": location,
                    "description": ad_data.get('body', 'No description.'),
                    "link": link,
                    "image_url": self._extract_image_url(ad_data)
                })
            return parsed_ads
        except Exception as e:
            self._debug_print(f"_parse_response error: {e}")
            return []

    def _extract_image_url(self, ad_data):
        images_list = ad_data.get('images', [])
        if not images_list: return None
            
        img_obj = images_list[0]
        cdn_base = img_obj.get('cdnBaseUrl')
        
        # Rule '-auto' allows Imgproxy to serve supported formats
        if cdn_base:
            return f"{cdn_base}?rule=gallery-desktop-1x-auto"
            
        url = img_obj.get('secureuri') or img_obj.get('uri') or img_obj.get('url')
        if not url and 'scale' in img_obj and len(img_obj['scale']) > 0:
            target_scale = img_obj['scale'][-1]
            url = target_scale.get('secureuri') or target_scale.get('uri') or target_scale.get('url')
            
        if url and url.startswith('//'):
            url = 'https:' + url
        return url

    def download_image(self, image_url):
        """Downloads the image from the CDN and strictly forces JPEG encoding in RAM."""
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