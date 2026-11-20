"""UI screenshot helper. Run inside the Playwright Docker image (see tools/screenshot.sh)."""
import sys
from playwright.sync_api import sync_playwright

url = sys.argv[1]
out = sys.argv[2]
category = sys.argv[3] if len(sys.argv) > 3 else ""
actions = sys.argv[4].split(",") if len(sys.argv) > 4 and sys.argv[4] else []

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1600, "height": 950})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
    pg.goto(url, wait_until="networkidle")
    if category:
        pg.get_by_text(category, exact=True).first.click()
        pg.wait_for_load_state("networkidle")
    for a in actions:
        pg.get_by_text(a).first.click()
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(300)
    pg.wait_for_timeout(500)
    pg.screenshot(path=out)
    b.close()
    print("errors:", errors)
