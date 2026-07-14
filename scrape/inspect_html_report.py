"""Inspect pagination and content structure of a raw HTML report.

Run:  uv run python scrape/inspect_html_report.py
"""

from bs4 import BeautifulSoup
from pathlib import Path

ROOT = Path(__file__).parent.parent
path = ROOT / "data" / "raw" / "hesa441_canadas_health_workforce_0023.html"

html = path.read_text(encoding="utf-8")
soup = BeautifulSoup(html, "lxml")

# Find navigation links (prev/next page, full-document)
print("=== Navigation links ===")
for a in soup.find_all("a", href=True):
    text = a.get_text(strip=True)
    href = a["href"]
    if any(kw in text.lower() for kw in ["next", "prev", "page", "full", "print", "all"]):
        print(f"  {text!r:30s} -> {href}")
    if any(kw in href.lower() for kw in ["page", "print", "full", "all"]):
        print(f"  {text!r:30s} -> {href}")

# Show content inside publication-container-content
print("\n=== publication-container-content ===")
content = soup.find(class_="publication-container-content")
if content:
    tags = [(t.name, t.get("class"), t.get_text(strip=True)[:100])
            for t in content.find_all(True) if t.get_text(strip=True)]
    print(f"  {len(tags)} tags with text")
    for name, cls, txt in tags[:50]:
        print(f"  <{name}> {txt!r}")

# Check if there's a print/full-text URL pattern
print("\n=== All hrefs containing 'report' or 'DocumentViewer' ===")
for a in soup.find_all("a", href=True):
    if "report" in a["href"].lower() or "document" in a["href"].lower():
        print(f"  {a.get_text(strip=True)!r:30s} -> {a['href']}")
