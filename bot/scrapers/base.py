"""
Filename: bot/scrapers/base.py
Description: Minimal common contract for classifieds-site scrapers. Introduced
             so bot/daemon.py can drive any site implementation (e.g. a future
             non-Subito scraper) without depending on site-specific internals.
             New site scrapers should subclass BaseScraper and live alongside
             subito.py in this package.

GNU GPLv3 Prelude:
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
"""

from abc import ABC, abstractmethod


class BaseScraper(ABC):
    """
    Common interface every site scraper must implement. `debug_mode` is a
    plain instance attribute (not a property) so subclasses can keep assigning
    to it the same way `SubitoScraper` does today (`scraper.debug_mode = True`).
    """

    debug_mode = False

    @abstractmethod
    def fetch_ads(self, url: str) -> list:
        """Fetches and returns a list of ad dicts for the given search URL."""
        raise NotImplementedError

    @abstractmethod
    def verify_waf_bypass(self):
        """Returns an (ok: bool, reason: str) tuple, used at daemon startup."""
        raise NotImplementedError

    @abstractmethod
    def download_image(self, image_url: str) -> bytes:
        """Downloads and returns the raw JPEG bytes for an ad's primary image."""
        raise NotImplementedError
