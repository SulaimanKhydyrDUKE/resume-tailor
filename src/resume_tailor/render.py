"""HTML -> PDF. The one genuinely deterministic step in the pipeline.

The old code drove Selenium + webdriver_manager and downloaded a ChromeDriver
on every cold start. Playwright is already a dependency here for the apply half,
so it renders too, and there is exactly one browser in the project.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

STYLES_DIR = Path(__file__).parent / "styles"

# ATS parsers read the PDF text layer. Anything that puts content into an image,
# a multi-column float, or a header/footer region tends to come back scrambled or
# missing, so print settings stay boring on purpose.
PDF_OPTIONS = {
    "format": "Letter",  # US applicant, US employers: the page size their tools and printers expect
    "print_background": True,
    "margin": {"top": "0.5in", "bottom": "0.5in", "left": "0.55in", "right": "0.55in"},
    "prefer_css_page_size": False,
}


def available_styles() -> dict[str, Path]:
    """Style name -> css path, read from the first-line comment of each file."""
    styles: dict[str, Path] = {}
    if not STYLES_DIR.is_dir():
        return styles
    for css in sorted(STYLES_DIR.glob("*.css")):
        first = css.read_text(encoding="utf-8").split("\n", 1)[0].strip()
        name = css.stem
        if first.startswith("/*") and first.endswith("*/"):
            content = first[2:-2].strip()
            name = content.split("$", 1)[0].strip() or css.stem
        styles[name] = css
    return styles


def _wrap(body_html: str, css: str, title: str, compact: int = 0) -> str:
    body_class = f' class="compact-{compact}"' if compact else ""
    if body_html.lstrip().startswith("<body"):
        body = body_html if not compact else body_html.replace("<body", f"<body{body_class}", 1)
    else:
        body = f"<body{body_class}>{body_html}</body>"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
{css}
</style>
</head>
{body}
</html>"""


MAX_COMPACT = 2  # notches the stylesheet defines (body.compact-1, body.compact-2)


async def render_pdf_async(
    body_html: str,
    out_path: str | Path,
    style: str = "clean",
    title: str = "Resume",
    compact: int = 0,
) -> Path:
    from playwright.async_api import async_playwright

    styles = available_styles()
    if style not in styles:
        if not styles:
            raise FileNotFoundError(f"No CSS themes found in {STYLES_DIR}")
        style = next(iter(styles))
    css = styles[style].read_text(encoding="utf-8")

    out_path = Path(out_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = _wrap(body_html, css, title, compact=compact)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="load")
            await page.emulate_media(media="print")
            await page.pdf(path=str(out_path), **PDF_OPTIONS)
        finally:
            await browser.close()
    return out_path


def render_pdf(body_html: str, out_path: str | Path, style: str = "clean", title: str = "Resume") -> Path:
    return asyncio.run(render_pdf_async(body_html, out_path, style, title))


async def render_to_fit_async(body_html: str, out_path: str | Path, style: str = "clean", title: str = "Resume",
                              max_pages: int = 1) -> tuple[Path, int, int]:
    """Render, and if the result runs past `max_pages`, render again at each
    tighter notch the stylesheet offers until it fits or the notches run out.
    Returns (path, pages, notch used)."""
    compact = 0
    while True:
        path = await render_pdf_async(body_html, out_path, style, title, compact=compact)
        pages = page_count(path)
        if pages <= max_pages or compact >= MAX_COMPACT:
            return path, pages, compact
        compact += 1


def extract_pdf_text(pdf_path: str | Path) -> str:
    """Read back what a parser would see.

    A resume that looks right and extracts wrong is the failure mode nobody
    catches by eye, so the pipeline verifies its own output rather than trusting
    the render. Headless Chromium cannot open a PDF (no viewer plugin), so this
    goes through pypdf, which is also closer to what an ATS actually runs.
    """
    from pypdf import PdfReader

    reader = PdfReader(str(Path(pdf_path).resolve()))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def page_count(pdf_path: str | Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(Path(pdf_path).resolve())).pages)
