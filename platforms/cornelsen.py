"""Cornelsen platform — OAuth 2.0 + PKCE auth, lossless PDF or tile download."""

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import time
import zipfile
from base64 import urlsafe_b64encode, b64decode
from io import BytesIO
from urllib.parse import urljoin

import fitz
import requests

from login_form import LoginFormParser
from downloader import download_pages
from pdf_builder import build_pdf
from ui import console, print_dim, make_progress, show_export_complete

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as crypto_padding
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

# ── Constants ────────────────────────────────────────────────────────────────

CLIENT_ID = "@!38C4.659F.8000.3A79!0001!7F12.03E3!0008!EC22.422D.7E51.7DE3"
REDIRECT_URI = "https://mein.cornelsen.de"
OIDC_SCOPE = "openid user_name roles cv_sap_kdnr cv_schule profile email meta inum tenant_id"
APP_ID = "uma_web_2023.18.3"

_AES_PARTS = b64decode("YWVzLTEyOC1jYmN8RCtEeEpTRn0yQjtrLTtDfQ==").decode("ascii").split("|")
AES_KEY = _AES_PARTS[1].encode("ascii")
AES_IV = AES_KEY[::-1]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

DISPLAY_NAME = "Cornelsen"

LICENSES_QUERY = """
query licenses {
  licenses {
    activeUntil
    isExpired
    canBeStarted
    coverUrl
    salesProduct {
      id url heading shortTitle subheading info coverUrl
      licenseModelId fullVersionId fullVersionUrl __typename
    }
    usageProduct {
      id url heading shortTitle subheading info coverUrl
      usagePlatformId __typename
    }
    __typename
  }
}"""

START_PRODUCT_MUTATION = """
mutation startProduct($productId: ProductId!) {
  startProduct(productId: $productId)
}"""


# ── Authentication ───────────────────────────────────────────────────────────

def authenticate(email, password):
    """Full OAuth 2.0 + PKCE flow. Returns id_token."""
    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    # Step 1: Visit cornelsen.de to get session cookies
    print_dim("Initializing session...")
    session.get("https://www.cornelsen.de/")
    session.get(f"https://www.cornelsen.de/shop/ccustomer/oauth/autoLogin?timestamp={int(time.time())}")

    # Step 2: Visit login page
    print_dim("Loading login page...")
    resp = session.get(
        "https://www.cornelsen.de/shop/ccustomer/oauth/login/",
        params={"afterAuthUrl": "https://www.cornelsen.de/"},
    )

    # Parse login form
    parser = LoginFormParser()
    parser.feed(resp.text)
    if not parser.action or not parser.username_field or not parser.password_field:
        raise RuntimeError("Could not parse login form — the page structure may have changed.")

    login_url = urljoin(str(resp.url), parser.action)
    form_data = parser.fields.copy()
    form_data[parser.username_field] = email
    form_data[parser.password_field] = password
    for btn_name, btn_value in parser.submit_fields:
        form_data[btn_name] = btn_value

    # Step 3: Submit credentials
    print_dim("Logging in...")
    resp = session.post(login_url, data=form_data)

    # Step 4: OAuth authorize with PKCE
    print_dim("Getting authorization code...")
    code_verifier = secrets.token_hex(48)
    code_challenge = (
        urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )

    authorize_params = {
        "scope": OIDC_SCOPE,
        "response_type": "code",
        "response_mode": "query",
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "state": secrets.token_hex(16),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }

    resp = session.get(
        "https://id.cornelsen.de/oxauth/restv1/authorize",
        params=authorize_params,
        allow_redirects=False,
    )
    location = resp.headers.get("Location", "")

    for _ in range(5):
        code_match = re.search(r"[?&]code=([^&]+)", location)
        if code_match:
            break
        if not location or resp.status_code not in (301, 302, 303):
            break
        resp = session.get(location, allow_redirects=False)
        location = resp.headers.get("Location", "")

    code_match = re.search(r"[?&]code=([^&]+)", location)
    if not code_match:
        raise RuntimeError("Login failed — could not get authorization code. Check your email and password.")
    code = code_match.group(1)

    # Step 5: Token exchange
    print_dim("Exchanging token...")
    resp = session.post(
        "https://id.cornelsen.de/oxauth/restv1/token",
        data={
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "code_verifier": code_verifier,
            "client_id": CLIENT_ID,
        },
    )
    resp.raise_for_status()
    return resp.json()["id_token"]


# ── Library ──────────────────────────────────────────────────────────────────

def _gql(id_token, operation, query, variables=None):
    resp = requests.post(
        "https://mein.cornelsen.de/bibliothek/api",
        headers={
            "Authorization": f"Bearer {id_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://mein.cornelsen.de",
            "User-Agent": UA,
        },
        json={
            "operationName": operation,
            "query": query,
            "variables": variables or {},
        },
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL error: {data['errors']}")
    return data["data"]


def fetch_library(auth):
    """Fetch available books via GraphQL. Returns list of book dicts."""
    id_token = auth
    licenses = _gql(id_token, "licenses", LICENSES_QUERY)["licenses"]

    books = []
    for lic in licenses:
        if lic.get("isExpired"):
            continue
        product = lic.get("usageProduct") or lic.get("salesProduct")
        if not product:
            continue
        books.append({
            "id": product["id"],
            "title": product.get("heading", product.get("shortTitle", "Unknown")),
            "subtitle": product.get("subheading", ""),
            "sales_id": (lic.get("salesProduct") or {}).get("id"),
        })

    if not books:
        raise RuntimeError("No active eBooks found in your library.")

    return books


def book_list_columns():
    return [("Title", "white"), ("Subtitle", "dim")]


def book_labels(books):
    return [(b["title"], b["subtitle"]) for b in books]


# ── Export ───────────────────────────────────────────────────────────────────

def export_book(book, auth, cfg):
    """Export a Cornelsen eBook — tries lossless first, falls back to tiles."""
    id_token = auth
    product_id = book["id"]
    sales_id = book.get("sales_id")
    book_title = book["title"]
    book_name = re.sub(r'[<>:"/\\|?*]', '_', book_title)
    method = cfg.get("method", "auto")

    ebooks_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "eBooks")
    os.makedirs(ebooks_dir, exist_ok=True)
    output_file = os.path.join(ebooks_dir, f"{book_name}.pdf")

    # Try lossless method first
    if method in ("auto", "lossless"):
        console.print()
        console.print("[bold]Trying lossless PDF download...[/bold]")
        progress = make_progress()

        with progress:
            ll_task = progress.add_task("[cyan]Lossless download", total=100)
            result = _try_lossless_download(id_token, product_id, progress, ll_task)
            if result is None and sales_id and sales_id != product_id:
                result = _try_lossless_download(id_token, sales_id, progress, ll_task)
            if result is not None:
                progress.update(ll_task, completed=100, description="[green]Decrypted")

        if result is not None:
            pdf_bytes, uma_data = result
            with open(output_file, "wb") as f:
                f.write(pdf_bytes)
            if uma_data:
                try:
                    _add_bookmarks(output_file, uma_data)
                except Exception:
                    pass
            size_mb = os.path.getsize(output_file) / (1024 * 1024)
            doc = fitz.open(output_file)
            total_pages = len(doc)
            doc.close()
            show_export_complete(output_file, total_pages, size_mb, extra="lossless")
            return

        if method == "lossless":
            raise RuntimeError("Lossless download not available for this book.")
        console.print("[yellow]  Lossless not available, falling back to image tiles...[/yellow]")

    # Tile method
    _export_tiles(id_token, product_id, book_title, book_name, output_file, cfg)


def _try_lossless_download(id_token, product_id, progress, task_id):
    """Try downloading encrypted PDF ZIP. Returns (pdf_bytes, uma_data) or None."""
    headers = {"Authorization": f"Bearer {id_token}", "User-Agent": UA}

    progress.update(task_id, description="[cyan]Getting download URL...")
    try:
        resp = requests.get(
            f"https://unterrichtsmanager.cornelsen.de/uma20/api/v2/umazip/{product_id}",
            headers=headers,
        )
        if resp.status_code != 200:
            return None
        zip_url = resp.json().get("url")
        if not zip_url:
            return None
    except Exception:
        return None

    progress.update(task_id, description="[cyan]Downloading encrypted PDF...")
    try:
        resp = requests.get(zip_url, headers={"User-Agent": UA}, stream=True)
        if resp.status_code != 200:
            return None
        total_size = int(resp.headers.get("content-length", 0))
        chunks = []
        downloaded = 0
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            chunks.append(chunk)
            downloaded += len(chunk)
            if total_size:
                progress.update(task_id, completed=int(downloaded / total_size * 100))
        zip_data = BytesIO(b"".join(chunks))
    except Exception:
        return None

    progress.update(task_id, description="[cyan]Decrypting PDF...")
    try:
        with zipfile.ZipFile(zip_data) as zf:
            names = zf.namelist()
            pdf_name = None
            for name in names:
                if name.endswith("_sf.pdf"):
                    pdf_name = name
                    break
            if not pdf_name:
                for name in names:
                    if name.endswith(".pdf"):
                        pdf_name = name
                        break
            if not pdf_name:
                return None

            uma_data = None
            if "uma.json" in names:
                uma_data = json.loads(zf.read("uma.json"))

            encrypted = zf.read(pdf_name)
            pdf_bytes = _decrypt_pdf(encrypted)
            return pdf_bytes, uma_data
    except Exception as e:
        console.print(f"[yellow]  Lossless method failed: {e}[/yellow]")
        return None


def _decrypt_pdf(encrypted_data):
    if not HAS_CRYPTO:
        raise ImportError("The 'cryptography' package is required for lossless PDF download.")
    cipher = Cipher(algorithms.AES(AES_KEY), modes.CBC(AES_IV))
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(encrypted_data) + decryptor.finalize()
    unpadder = crypto_padding.PKCS7(128).unpadder()
    try:
        decrypted = unpadder.update(decrypted) + unpadder.finalize()
    except Exception:
        pass
    if not decrypted[:5].startswith(b"%PDF"):
        raise ValueError("Decryption produced invalid PDF")
    return decrypted


def _add_bookmarks(pdf_path, uma_data):
    locations = uma_data.get("location", [])
    if not locations:
        return
    doc = fitz.open(pdf_path)
    toc = []

    def walk(items, level=1):
        for item in items:
            title = item.get("title", item.get("name", ""))
            page = item.get("page", item.get("pageIndex"))
            if title and isinstance(page, int):
                toc.append([level, title, page + 1])
            children = item.get("children", [])
            if children:
                walk(children, level + 1)

    walk(locations)
    if toc:
        doc.set_toc(toc)
        doc.save(pdf_path, incremental=True, encryption=fitz.PDF_ENCRYPT_NONE)
    doc.close()


def _export_tiles(id_token, product_id, book_title, book_name, output_file, cfg):
    """Fallback: download page tiles from PSPDFKit and build PDF."""
    quality = cfg.get("quality", 4)
    max_concurrent = cfg.get("max_concurrent_downloads", 10)
    pages_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_pages_tmp")

    console.print()
    console.print("[bold]Preparing tile download...[/bold]")

    # Start product
    viewer_url = _gql(id_token, "startProduct", START_PRODUCT_MUTATION, {"productId": product_id})["startProduct"]

    # PSPDFKit version
    print_dim("Fetching PSPDFKit version...")
    pspdfkit_version = _get_pspdfkit_version(id_token, viewer_url)

    # Book data
    print_dim("Fetching book data...")
    book_data = _fetch_book_data(id_token, product_id)

    # PSPDFKit auth
    print_dim("Authenticating with PSPDFKit...")
    pspdfkit_auth = _get_pspdfkit_auth(id_token, book_data, pspdfkit_version)

    # Document info
    print_dim("Fetching document info...")
    doc_info = _fetch_document_info(pspdfkit_auth, pspdfkit_version)

    pages = doc_info.get("data", doc_info).get("pages", [])
    total = len(pages)

    # Show config
    from rich.table import Table
    from rich import box
    table = Table(box=box.ROUNDED, title="[bold]Configuration[/bold]", title_style="cyan")
    table.add_column("Setting", style="dim")
    table.add_column("Value", style="white")
    table.add_row("Book", book_title)
    table.add_row("Output", f"[bold green]eBooks/{book_name}.pdf[/bold green]")
    table.add_row("Pages", str(total))
    table.add_row("Quality", str(quality))
    table.add_row("Concurrency", str(max_concurrent))
    console.print(table)
    console.print()

    if os.path.exists(pages_dir):
        shutil.rmtree(pages_dir)

    isbn = pspdfkit_auth["ebook_isbn"]
    layer = pspdfkit_auth["layer_handle"]

    img_headers = {
        "x-pspdfkit-image-token": pspdfkit_auth["image_token"],
        "Accept": "image/webp,*/*",
        "Referer": "https://ebook.cornelsen.de/",
        "User-Agent": UA,
    }
    text_headers = {
        "X-PSPDFKit-Token": pspdfkit_auth["token"],
        "Referer": "https://ebook.cornelsen.de/",
        "User-Agent": UA,
    }
    if pspdfkit_version:
        text_headers["pspdfkit-version"] = pspdfkit_version

    # Prepare download tasks
    download_tasks = []
    for i, page in enumerate(pages):
        page_dir = os.path.join(pages_dir, f"page_{i:04d}")
        os.makedirs(page_dir, exist_ok=True)
        w = int(page["width"] * quality)
        h = int(page["height"] * quality)
        img_url = f"https://pspdfkit.prod.cornelsen.de/i/d/{isbn}/h/{layer}/page-{i}-dimensions-{w}-{h}-tile-0-0-{w}-{h}"
        text_url = f"https://pspdfkit.prod.cornelsen.de/i/d/{isbn}/h/{layer}/page-{i}-text"
        download_tasks.append((img_url, os.path.join(page_dir, "page.webp"), img_headers))
        download_tasks.append((text_url, os.path.join(page_dir, "text.json"), text_headers))

    progress = make_progress()
    with progress:
        # Phase 1: Download
        dl_task = progress.add_task("[cyan]Downloading pages", total=len(download_tasks))
        failed = asyncio.run(download_pages(
            download_tasks,
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
        pages_data = _prepare_tile_pages_data(pages_dir, pages, quality)
        build_pdf(pages_data, output_file, progress=progress, progress_task=pdf_task)
        progress.update(pdf_task, description="[green]PDF complete")

    shutil.rmtree(pages_dir, ignore_errors=True)

    size_mb = os.path.getsize(output_file) / (1024 * 1024)
    show_export_complete(output_file, total, size_mb)


def _fetch_book_data(id_token, product_id):
    resp = requests.get(
        f"https://ebook.cornelsen.de/uma20/api/v2/umas/{product_id}",
        headers={
            "Authorization": f"Bearer {id_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "User-Agent": UA,
        },
    )
    resp.raise_for_status()
    return resp.json()


def _get_pspdfkit_version(id_token, viewer_url):
    try:
        resp = requests.get(viewer_url, headers={"Authorization": f"Bearer {id_token}", "User-Agent": UA})
        js_match = re.search(r'(?:https://ebook\.cornelsen\.de/)?(main\.[^"\']+\.js)', resp.text)
        if not js_match:
            return None
        js_url = f"https://ebook.cornelsen.de/{js_match.group(1)}"
        resp = requests.get(js_url, headers={"User-Agent": UA})
        version_match = re.search(r'protocol=.*, client=.*, client-git=[^\s"]*', resp.text)
        if version_match:
            return version_match.group(0)
    except Exception:
        pass
    return None


def _get_pspdfkit_auth(id_token, book_data, pspdfkit_version=None):
    module_isbn = book_data.get("module", {}).get("moduleIsbn")
    ebook_isbn = book_data.get("ebookIsbnSbNum")
    if not module_isbn or not ebook_isbn:
        raise RuntimeError("Could not find book ISBNs in metadata")

    resp = requests.get(
        f"https://ebook.cornelsen.de/uma20/api/v2/pspdfkitjwt/{module_isbn}/{ebook_isbn}",
        headers={
            "Accept": "application/json",
            "x-cv-app-identifier": APP_ID,
            "Authorization": f"Bearer {id_token}",
            "User-Agent": UA,
        },
    )
    resp.raise_for_status()
    pspdfkit_jwt = resp.text.strip().strip('"')

    headers = {
        "pspdfkit-platform": "web",
        "Referer": "https://ebook.cornelsen.de/",
        "Origin": "https://ebook.cornelsen.de",
        "User-Agent": UA,
    }
    if pspdfkit_version:
        headers["pspdfkit-version"] = pspdfkit_version

    resp = requests.post(
        f"https://pspdfkit.prod.cornelsen.de/i/d/{ebook_isbn}/auth",
        json={"jwt": pspdfkit_jwt, "origin": f"https://ebook.cornelsen.de/{module_isbn}/start"},
        headers=headers,
    )
    resp.raise_for_status()
    auth = resp.json()

    return {
        "token": auth["token"],
        "image_token": auth["imageToken"],
        "layer_handle": auth["layerHandle"],
        "ebook_isbn": ebook_isbn,
    }


def _fetch_document_info(pspdfkit_auth, pspdfkit_version=None):
    isbn = pspdfkit_auth["ebook_isbn"]
    layer = pspdfkit_auth["layer_handle"]
    headers = {
        "X-PSPDFKit-Token": pspdfkit_auth["token"],
        "pspdfkit-platform": "web",
        "User-Agent": UA,
    }
    if pspdfkit_version:
        headers["pspdfkit-version"] = pspdfkit_version

    resp = requests.get(
        f"https://pspdfkit.prod.cornelsen.de/i/d/{isbn}/h/{layer}/document.json",
        headers=headers,
    )
    resp.raise_for_status()
    return resp.json()


def _prepare_tile_pages_data(pages_dir, pages, quality):
    """Convert Cornelsen tile data into the format expected by pdf_builder."""
    pages_data = []

    for i, page_info in enumerate(pages):
        page_dir = os.path.join(pages_dir, f"page_{i:04d}")
        img_path = os.path.join(page_dir, "page.webp")
        text_path = os.path.join(page_dir, "text.json")

        page_data = {"image_path": img_path, "text_boxes": None, "links": None}

        if os.path.exists(text_path) and os.path.exists(img_path):
            try:
                with open(text_path) as f:
                    text_data = json.load(f)

                native_w = page_info["width"]
                native_h = page_info["height"]

                # Get actual PDF page dimensions
                img = fitz.open(img_path)
                pdfbytes = img.convert_to_pdf()
                img.close()
                img_pdf = fitz.open("pdf", pdfbytes)
                pw = img_pdf[0].rect.width
                ph = img_pdf[0].rect.height
                img_pdf.close()

                scale_x = pw / native_w
                scale_y = ph / native_h

                text_boxes = []
                for line in text_data.get("textLines", []):
                    text = line.get("contents", "").strip()
                    if not text:
                        continue
                    bbox = line.get("boundingBox", {})
                    if isinstance(bbox, list):
                        x = bbox[0] * scale_x
                        y = bbox[1] * scale_y
                        h = (bbox[3] - bbox[1]) * scale_y
                    else:
                        x = bbox.get("left", 0) * scale_x
                        y = bbox.get("top", 0) * scale_y
                        h = bbox.get("height", 12) * scale_y
                    text_boxes.append({"x": x, "y": y, "w": 0, "h": h, "text": text})

                if text_boxes:
                    page_data["text_boxes"] = text_boxes
            except Exception:
                pass

        pages_data.append(page_data)

    return pages_data
