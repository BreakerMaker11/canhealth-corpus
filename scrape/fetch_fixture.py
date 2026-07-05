"""One-shot script: fetch the HESA 'Canada's Health Workforce' study pages and save to fixtures/.

Run once:  uv run python scrape/fetch_fixture.py
Never re-run against live site if fixtures already exist.
"""

import sys
import time
from pathlib import Path
import requests

HEADERS = {"User-Agent": "canhealth-corpus/0.1 (personal research; contact: syermakou@gmail.com)"}
FIXTURES = Path(__file__).parent.parent / "fixtures"
DELAY = 1.0

TARGETS = [
    (
        "study_workforce.html",
        "https://www.ourcommons.ca/committees/en/HESA/StudyActivity?studyActivityId=11516538",
    ),
    (
        "study_workforce_briefs.html",
        "https://www.ourcommons.ca/committees/en/HESA/StudyActivity/GetBriefs?studyActivityId=11516538",
    ),
    (
        "hesa441_report10.html",
        "https://www.ourcommons.ca/DocumentViewer/en/44-1/HESA/report-10/",
    ),
    (
        "hesa441_work_tab.html",
        "https://www.ourcommons.ca/Committees/en/HESA/Work?parl=44&session=1",
    ),
]


def fetch_once(filename: str, url: str) -> None:
    dest = FIXTURES / filename
    if dest.exists():
        print(f"SKIP  {filename} (already exists)")
        return
    print(f"GET   {url}")
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    dest.write_text(r.text, encoding="utf-8")
    print(f"SAVED {dest}  ({len(r.text):,} chars)")
    time.sleep(DELAY)


if __name__ == "__main__":
    FIXTURES.mkdir(exist_ok=True)
    for name, url in TARGETS:
        fetch_once(name, url)
    print("Done.")
