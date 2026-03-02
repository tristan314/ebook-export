"""Shared PDF assembly — image background + invisible text overlay + links."""

import fitz  # PyMuPDF


def build_pdf(pages_data, output_path, progress=None, progress_task=None):
    """Build a PDF from page data.

    Args:
        pages_data: list of dicts, each with:
            - image_path: str — path to page image file
            - text_boxes: list of {x, y, w, h, text} dicts (PDF coordinates)
                          OR None to skip text layer
            - links: list of {rect: fitz.Rect, target_page: int} dicts
                     OR None to skip links
        output_path: str — output PDF file path
        progress: Rich Progress instance (optional)
        progress_task: Rich task ID (optional)

    Returns:
        output_path
    """
    import os
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    doc = fitz.open()
    pending_links = []

    for page_idx, page_data in enumerate(pages_data):
        img_path = page_data["image_path"]

        if not os.path.exists(img_path):
            if progress and progress_task is not None:
                progress.update(progress_task, advance=1)
            continue

        # Add page with image
        img = fitz.open(img_path)
        pdfbytes = img.convert_to_pdf()
        img.close()
        img_pdf = fitz.open("pdf", pdfbytes)
        page = doc.new_page(width=img_pdf[0].rect.width, height=img_pdf[0].rect.height)
        page.show_pdf_page(page.rect, img_pdf, 0)
        img_pdf.close()

        # Invisible text overlay
        text_boxes = page_data.get("text_boxes")
        if text_boxes:
            tw = fitz.TextWriter(page.rect)
            for box in text_boxes:
                font_size = box["h"] * 0.85
                if font_size < 1:
                    font_size = 1
                pos = (box["x"], box["y"] + box["h"] * 0.85)
                try:
                    tw.append(pos, box["text"], fontsize=font_size)
                except Exception:
                    pass
            tw.write_text(page, render_mode=3)

        # Collect links (deferred — target pages may not exist yet)
        links = page_data.get("links")
        if links:
            for link in links:
                pending_links.append((page_idx, link["rect"], link["target_page"]))

        if progress and progress_task is not None:
            progress.update(progress_task, advance=1)

    # Insert links now that all pages exist
    if progress and progress_task is not None:
        progress.update(progress_task, description="[cyan]Adding links...")
    for page_idx, rect, target_page in pending_links:
        if 0 <= target_page < len(doc):
            doc[page_idx].insert_link({
                "kind": fitz.LINK_GOTO,
                "from": rect,
                "page": target_page,
            })

    if progress and progress_task is not None:
        progress.update(progress_task, description="[cyan]Saving PDF...")
    doc.save(output_path, garbage=0, deflate=False)
    doc.close()

    return output_path
