"""Klett platform — Keycloak auth, bridge.klett.de download, PDF assembly."""

import asyncio
import os
import re
import shutil
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import fitz
import requests

from login_form import LoginFormParser
from downloader import download_pages
from pdf_builder import build_pdf
from ui import console, print_dim, make_progress, show_export_complete

# ── Constants ────────────────────────────────────────────────────────────────

KEYCLOAK_BASE = "https://id.klett.de/auth/realms/ekv"
AUTH_ENDPOINT = f"{KEYCLOAK_BASE}/protocol/openid-connect/auth"
TOKEN_ENDPOINT = f"{KEYCLOAK_BASE}/protocol/openid-connect/token"
CLIENT_ID = "arbeitsplatz-app"
REDIRECT_URI = "https://arbeitsplatz.klett.de/"

API_BASE = "https://api.klett.de"
BRIDGE_BASE = "https://bridge.klett.de"

SVG_WIDTH = 768
SVG_HEIGHT = 1024
BASE_URL = "https://bridge.klett.de/{}/content/pages/page_{}/{}"
DATA_URL = "https://bridge.klett.de/{}/data.json"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

DISPLAY_NAME = "Klett"


# ── Authentication ───────────────────────────────────────────────────────────

def authenticate(email, password):
    """Keycloak OIDC login. Returns access_token."""
    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    # Step 1: Visit Keycloak auth URL
    print_dim("Loading login page...")
    auth_params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "openid",
    }
    resp = session.get(AUTH_ENDPOINT, params=auth_params, allow_redirects=True)

    # Step 2: Parse login form
    parser = LoginFormParser(form_id="kc-form-login")
    parser.feed(resp.text)
    if not parser.action or not parser.username_field or not parser.password_field:
        # Retry without form_id restriction
        parser = LoginFormParser()
        parser.feed(resp.text)
    if not parser.action or not parser.username_field or not parser.password_field:
        raise RuntimeError("Could not parse Keycloak login form — the page structure may have changed.")

    login_url = parser.action
    if not login_url.startswith("http"):
        login_url = urljoin(str(resp.url), login_url)

    form_data = parser.fields.copy()
    form_data[parser.username_field] = email
    form_data[parser.password_field] = password
    for btn_name, btn_value in parser.submit_fields:
        form_data[btn_name] = btn_value

    # Step 3: Submit credentials
    print_dim("Logging in...")
    resp = session.post(login_url, data=form_data, allow_redirects=True)
    resp = session.get(AUTH_ENDPOINT, params=auth_params, allow_redirects=False)
    location = resp.headers.get("Location", "")
    code = None

    for _ in range(10):
        code_match = re.search(r"[?&]code=([^&]+)", location)
        if code_match:
            code = code_match.group(1)
            break
        if not location or resp.status_code not in (301, 302, 303):
            break
        resp = session.get(location, allow_redirects=False)
        location = resp.headers.get("Location", "")

    if not code:
        raise RuntimeError("Login failed — could not get authorization code. Check your email and password.")

    # Step 4: Exchange code for access_token
    resp = session.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code": code,
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ── Library ──────────────────────────────────────────────────────────────────

def fetch_library(auth):
    """Fetch available books from api.klett.de. Returns list of book dicts."""
    access_token = auth
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": UA,
    }
    all_books = []
    offset = 0
    limit = 50

    while True:
        resp = requests.get(
            f"{API_BASE}/licenses/subjects",
            params={
                "by": "LICENSE",
                "available": "true",
                "limit": limit,
                "offset": offset,
                "sort": "-license.assigned_at",
            },
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        all_books.extend(data.get("contents", []))
        total = data.get("total", 0)
        if offset + limit >= total:
            break
        offset += limit

    # Filter to eBooks and resolve titles
    ebooks = []
    for b in all_books:
        dienst = b.get("dienst", {})
        dienst_value = dienst.get("value", "") if isinstance(dienst, dict) else ""
        if not dienst_value:
            continue
        ebooks.append({
            "id": dienst_value,
            "title": b.get("titel", ""),
            "subtitle": b.get("untertitel", ""),
            "produktnummer": b.get("produktnummer", ""),
        })

    if not ebooks:
        raise RuntimeError("No eBooks found in your library.")

    # Look up actual titles from klett.de
    print_dim("Looking up book titles...")
    for b in ebooks:
        if b["produktnummer"]:
            title = _fetch_product_title(b["produktnummer"])
            if title:
                b["title"] = title
        if not b["title"]:
            b["title"] = b["produktnummer"] or b["id"]

    return ebooks


def _fetch_product_title(produktnummer):
    """Look up human-readable title from klett.de product page."""
    try:
        resp = requests.get(
            f"https://www.klett.de/produkt/isbn/{produktnummer}",
            headers={"User-Agent": UA},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        m = re.search(r"<title>\s*Ernst Klett Verlag\s*-\s*(.+?)\s*Produktdetails\s*</title>", resp.text, re.S)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return None


def book_list_columns():
    """Return column definitions for select_from_list."""
    return [("Title", "white"), ("Subtitle", "dim"), ("ID", "dim")]


def book_labels(books):
    """Return label tuples for select_from_list."""
    return [(b["title"], b["subtitle"], b["id"]) for b in books]


# ── Export ───────────────────────────────────────────────────────────────────

def export_book(book, auth, cfg):
    """Download pages and build PDF for a Klett book."""
    access_token = auth
    book_id = book["id"]
    book_title = book["title"]
    book_name = re.sub(r'[<>:"/\\|?*]', '_', book_title)
    scale = cfg.get("scale", 4)
    max_concurrent = cfg.get("max_concurrent_downloads", 10)

    ebooks_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "eBooks")
    os.makedirs(ebooks_dir, exist_ok=True)
    output_file = os.path.join(ebooks_dir, f"{book_name}.pdf")
    pages_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_pages_tmp")

    # Fetch book data
    console.print("[bold]Fetching book data...[/bold]")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Referer": f"{BRIDGE_BASE}/{book_id}/",
        "User-Agent": UA,
    }
    resp = requests.get(DATA_URL.format(book_id), headers=headers)
    resp.raise_for_status()
    data = resp.json()
    total = len(data["pages"])

    # Show config
    from rich.table import Table
    from rich import box
    table = Table(box=box.ROUNDED, title="[bold]Configuration[/bold]", title_style="cyan")
    table.add_column("Setting", style="dim")
    table.add_column("Value", style="white")
    table.add_row("Book", book_title)
    table.add_row("Output", f"[bold green]eBooks/{book_name}.pdf[/bold green]")
    table.add_row("Pages", str(total))
    table.add_row("Scale", f"Scale{scale}")
    table.add_row("Concurrency", str(max_concurrent))
    console.print(table)
    console.print()

    # Clean pages dir
    if os.path.exists(pages_dir):
        shutil.rmtree(pages_dir)

    # Prepare download tasks
    download_tasks = []
    for i in range(total):
        page_dir = os.path.join(pages_dir, f"page_{i:03d}")
        os.makedirs(page_dir, exist_ok=True)
        img_url = BASE_URL.format(book_id, i, f"Scale{scale}.png")
        svg_url = BASE_URL.format(book_id, i, "searchwords.svg")
        download_tasks.append((img_url, os.path.join(page_dir, f"Scale{scale}.png")))
        download_tasks.append((svg_url, os.path.join(page_dir, "searchwords.svg")))

    session_headers = {
        "Authorization": f"Bearer {access_token}",
        "Referer": f"{BRIDGE_BASE}/{book_id}/",
        "User-Agent": UA,
    }

    progress = make_progress()
    with progress:
        # Phase 1: Download
        dl_task = progress.add_task("[cyan]Downloading pages", total=len(download_tasks))
        failed = asyncio.run(download_pages(
            download_tasks,
            session_headers=session_headers,
            max_concurrent=max_concurrent,
            progress=progress,
            progress_task=dl_task,
        ))
        if failed:
            progress.update(dl_task, description=f"[yellow]Downloaded ({len(failed)} failed)")
        else:
            progress.update(dl_task, description="[green]Download complete")

        # Phase 2: Build PDF
        pdf_task = progress.add_task("[cyan]Building PDF", total=total)
        pages_data = _prepare_pages_data(pages_dir, data, total, scale)
        build_pdf(pages_data, output_file, progress=progress, progress_task=pdf_task)
        progress.update(pdf_task, description="[green]PDF complete")

    # Clean up
    shutil.rmtree(pages_dir, ignore_errors=True)

    size_mb = os.path.getsize(output_file) / (1024 * 1024)
    show_export_complete(output_file, total, size_mb)


def _prepare_pages_data(pages_dir, data, total, scale):
    """Convert Klett page data into the format expected by pdf_builder."""
    pages_data = []

    for i in range(total):
        page_dir = os.path.join(pages_dir, f"page_{i:03d}")
        img_path = os.path.join(page_dir, f"Scale{scale}.png")
        svg_path = os.path.join(page_dir, "searchwords.svg")

        page_data = {"image_path": img_path, "text_boxes": None, "links": None}

        # Parse SVG word boxes + text from data.json
        page_info = data["pages"][i]
        text = page_info["content"].get("text", "")
        words = text.split()

        if words and os.path.exists(svg_path) and os.path.exists(img_path):
            boxes = _parse_svg_word_boxes(svg_path)
            # Need page dimensions to scale SVG coords → PDF coords
            img = fitz.open(img_path)
            pdfbytes = img.convert_to_pdf()
            img.close()
            img_pdf = fitz.open("pdf", pdfbytes)
            pw = img_pdf[0].rect.width
            ph = img_pdf[0].rect.height
            img_pdf.close()

            scale_x = pw / SVG_WIDTH
            scale_y = ph / SVG_HEIGHT

            text_boxes = []
            for word, (bx, by, bw, bh) in zip(words, boxes):
                text_boxes.append({
                    "x": bx * scale_x,
                    "y": by * scale_y,
                    "w": bw * scale_x,
                    "h": bh * scale_y,
                    "text": word,
                })
            page_data["text_boxes"] = text_boxes

        # Internal page links from layer0
        if "layers" in page_info and os.path.exists(img_path):
            # Get page dimensions if not already
            if not page_data["text_boxes"]:
                img = fitz.open(img_path)
                pdfbytes = img.convert_to_pdf()
                img.close()
                img_pdf = fitz.open("pdf", pdfbytes)
                pw = img_pdf[0].rect.width
                ph = img_pdf[0].rect.height
                img_pdf.close()

            links = []
            for layer in page_info["layers"]:
                if layer["layer"] != "layer0":
                    continue
                for area in layer.get("areas", []):
                    url = area.get("url", "")
                    m = re.match(r"\?page=(\d+)", url)
                    if not m:
                        continue
                    target_page = int(m.group(1)) - 1
                    if target_page < 0 or target_page >= total:
                        continue
                    x = area["x"] * pw
                    y = area["y"] * ph
                    w = area["width"] * pw
                    h = area["height"] * ph
                    links.append({"rect": fitz.Rect(x, y, x + w, y + h), "target_page": target_page})
            if links:
                page_data["links"] = links

        pages_data.append(page_data)

    return pages_data


def _parse_svg_word_boxes(svg_path):
    """Parse SVG path elements into (x, y, w, h) tuples."""
    tree = ET.parse(svg_path)
    root = tree.getroot()
    boxes = []
    for path in root.iter("{http://www.w3.org/2000/svg}path"):
        d = path.get("d", "")
        match = re.match(r"M([\d.]+)\s+([\d.]+)l([\d.]+)\s+0l0\s+([\d.]+)", d)
        if match:
            boxes.append(tuple(float(v) for v in match.groups()))
    return boxes
