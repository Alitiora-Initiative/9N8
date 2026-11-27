"""Scrapy JSON-LD metadata spider for an explicitly allowlisted set of public pages.

Use this for permitted historical metadata collection only. Scrapy's robots.txt
respect and download throttling are intentionally enabled. No proxy rotation or
user-agent spoofing is used to bypass a provider's controls.
"""
import json
import scrapy


class PublicMetadataSpider(scrapy.Spider):
    name = "n8_public_metadata"
    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "DOWNLOAD_DELAY": 2.0,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 2.0,
        "AUTOTHROTTLE_MAX_DELAY": 30.0,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "USER_AGENT": "9N8ResearchBot/1.0 (+contact: operator-configured)",
    }

    def __init__(self, urls="", allowed_domains="", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.start_urls = [value.strip() for value in urls.split(",") if value.strip()]
        self.allowed_domains = [value.strip().lower() for value in allowed_domains.split(",") if value.strip()]
        if not self.start_urls or not self.allowed_domains:
            raise ValueError("Supply both comma-separated urls and allowed_domains")

    def parse(self, response):
        if response.url.split(":", 1)[0].lower() != "https":
            return
        for node in response.css('script[type="application/ld+json"]::text').getall():
            try:
                data = json.loads(node)
            except json.JSONDecodeError:
                continue
            records = data if isinstance(data, list) else [data]
            for record in records:
                if isinstance(record, dict):
                    yield {
                        "source_url": response.url,
                        "retrieved_at_utc": response.headers.get("Date", b"").decode("ascii", "ignore"),
                        "json_ld": record,
                    }
