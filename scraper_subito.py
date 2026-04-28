"""
Filename: scraper_subito.py
Version: 3.1.0
Date: 2026-04-28
Author: Leonardo Lisa
Description: Target-specific Web Scraper for Subito.it. Implements WAF evasion (Safari 15.3), Next.js JSON parsing, and image extraction.
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
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup
from PIL import Image
from io import BytesIO

class SubitoScraper:
    def __init__(self):
        self.base_url = "https://www.subito.it/"
        self.debug_mode = False
        # Safari 15.3 footprint effectively bypasses DataDome HTTP 403 blocks
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
        """Fetches the target URL and extracts standardized ad dictionaries."""
        try:
            res = self.session.get(url, timeout=15)
            if res.status_code != 200:
                self._debug_print(f"HTTP {res.status_code} for URL: {url}")
                return []
                
            soup = BeautifulSoup(res.text, 'html.parser')
            script_tag = soup.find('script', id='__NEXT_DATA__')
            if not script_tag: 
                self._debug_print("__NEXT_DATA__ JSON payload not found in HTML.")
                return []
                
            json_data = json.loads(script_tag.string)
            
            # Subito.it JSON schema points to 'originalList'
            items_node = json_data.get('props', {}).get('pageProps', {}).get('initialState', {}).get('items', {})
            items_list = items_node.get('originalList', [])
            
            parsed_ads = []
            for product in items_list:
                ad_data = product.get('item', product)
                link = ad_data.get('urls', {}).get('default', '')
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
            self._debug_print(f"fetch_ads exception: {e}")
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