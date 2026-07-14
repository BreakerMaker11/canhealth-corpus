"""Explore fixture HTML to understand the structure before writing the parser.

Run:  uv run python scrape/explore_fixture.py
"""

from pathlib import Path
from bs4 import BeautifulSoup

FIXTURES = Path(__file__).parent.parent / "fixtures"


def explore_study_page() -> None:
    html = (FIXTURES / "study_workforce.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")

    print("=" * 60)
    print("STUDY PAGE: study_workforce.html")
    print("=" * 60)

    # Title / study name
    h1 = soup.find("h1")
    print(f"\nH1: {h1.get_text(strip=True) if h1 else 'not found'}")

    # Breadcrumb for parliament/session context
    breadcrumbs = soup.select(".breadcrumb li")
    print(f"\nBreadcrumbs: {[b.get_text(strip=True) for b in breadcrumbs]}")

    # Section headings (accordion panels)
    panels = soup.select(".panel-heading")
    print(f"\nAccordion panels ({len(panels)}):")
    for p in panels:
        print(f"  - {p.get_text(strip=True)[:80]}")

    # The briefs accordion trigger
    briefs_trigger = soup.find("a", href="#briefs-submitted-item")
    if briefs_trigger:
        print(f"\nBriefs trigger text: {briefs_trigger.get_text(strip=True)}")

    # Report/gov-response links on the main page
    print("\nDocumentViewer links (reports/gov responses):")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "DocumentViewer" in href or "documentviewer" in href.lower():
            print(f"  {href!r:90s} -> {a.get_text(strip=True)[:60]}")


def explore_briefs_fragment() -> None:
    html = (FIXTURES / "study_workforce_briefs.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")

    print("\n" + "=" * 60)
    print("BRIEFS FRAGMENT: study_workforce_briefs.html")
    print("=" * 60)

    items = soup.select(".brief-item")
    print(f"\nTotal .brief-item elements: {len(items)}")

    if not items:
        print("No .brief-item found — dumping all class names in fragment:")
        for tag in soup.find_all(True):
            cls = tag.get("class")
            if cls:
                print(f"  <{tag.name} class={cls}>")
        return

    print(f"\n--- First item raw HTML ---\n{items[0]}")
    print(f"\n--- Second item raw HTML ---\n{items[1]}")

    print("\n--- All items parsed fields ---")
    for i, item in enumerate(items):
        name_el = item.select_one(".name")
        pdf_link = item.select_one("a.btn-brief")
        joint_el = item.select_one(".joint-name")
        published_el = item.select_one(".published")

        # Org name: first direct text node inside .name (skip the calendar <a>)
        if name_el:
            org = "".join(t for t in name_el.strings
                          if t.parent == name_el or t.parent.name not in ("a", "i")).strip()
        else:
            org = "?"

        pdf_href = pdf_link["href"] if pdf_link else "MISSING"
        pub_text = published_el.get_text(strip=True) if published_el else "?"
        joint = joint_el.get_text(strip=True)[:60] if joint_el else ""

        print(f"  [{i:02d}] org={org!r:50s}  joint={joint!r}")
        print(f"       pdf={pdf_href}")
        print(f"       pub={pub_text}")
        print()


def explore_report_page() -> None:
    html = (FIXTURES / "hesa441_report10.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")

    print("\n" + "=" * 60)
    print("REPORT PAGE: hesa441_report10.html")
    print("=" * 60)

    h1 = soup.find("h1")
    print(f"\nH1: {h1.get_text(strip=True)[:120] if h1 else 'not found'}")

    # Look for PDF download links
    print("\nPDF/download links:")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(x in href.lower() for x in [".pdf", "download", "print"]):
            print(f"  {href!r:80s} -> {a.get_text(strip=True)[:60]}")

    # Check if the main content is inline HTML
    content_div = soup.select_one(".publication-anchor") or soup.select_one("#publication") or soup.select_one(".WordSection1")
    if content_div:
        text_sample = content_div.get_text(strip=True)[:300]
        print(f"\nInline text content found ({len(content_div.get_text())} chars):\n  {text_sample}")
    else:
        print("\nNo standard content div found — listing candidate divs with id/class:")
        for tag in soup.find_all(["div", "section", "article"], id=True):
            print(f"  <{tag.name} id={tag['id']!r}>  {tag.get_text(strip=True)[:60]}")


def explore_work_tab() -> None:
    html = (FIXTURES / "hesa441_work_tab.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")

    print("\n" + "=" * 60)
    print("WORK TAB: hesa441_work_tab.html")
    print("=" * 60)

    study_links = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "StudyActivity?studyActivityId=" in href:
            sid = href.split("studyActivityId=")[1]
            title = a.get_text(strip=True)
            if sid not in study_links and title:
                study_links[sid] = (href, title)

    print(f"\nUnique studies found: {len(study_links)}")
    for sid, (href, title) in study_links.items():
        print(f"  {sid:>12s}  {title[:80]}")


if __name__ == "__main__":
    explore_study_page()
    explore_briefs_fragment()
    explore_report_page()
    explore_work_tab()
