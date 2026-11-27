"""Optional Selenium browser smoke test for a deployed or local 9N8 Mini App.

This helper performs ordinary page navigation only. It does not bypass access
controls, automate trades or interact with wallet signing dialogs.
"""
import argparse
from selenium import webdriver
from selenium.webdriver.chrome.options import Options


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--expected-title", default="9N8")
    args = parser.parse_args()
    if not args.url.startswith("https://") and not args.url.startswith("http://127.0.0.1"):
        parser.error("URL must be HTTPS or a local 127.0.0.1 test server")
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=options)
    try:
        driver.set_page_load_timeout(25)
        driver.get(args.url)
        title = driver.title
        if args.expected_title.lower() not in title.lower():
            raise RuntimeError(f"unexpected document title: {title!r}")
        print("9N8 Selenium page smoke test: PASS")
        return 0
    finally:
        driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
