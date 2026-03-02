"""Shared PDF assembly — image background + invisible text overlay + links."""

import fitz  # PyMuPDF


def build_pdf(pages_data, output_path, progress=None, progress_task=None):
    """Build a PDF from page data.

    Args:
        pages_data: list of dicts, each with:
            - image_path: str — path to page image file
            - text_boxes: list of {x, y, w, h, text} dicts
                          OR None to skip text layer
            - text_ref_size: (ref_w, ref_h) tuple — if present, text_boxes
                             coordinates are in this reference space and will
                             be scaled to actual page dimensions
            - links: list of link dicts, OR None to skip links.
                     Each link is either:
                       {rect: fitz.Rect, target_page: int}  (absolute coords)
                     or:
                       {from_frac: (x0, y0, x1, y1), target_page: int}  (0-1 fractions)
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
        pw = img_pdf[0].rect.width
        ph = img_pdf[0].rect.height
        page = doc.new_page(width=pw, height=ph)
        page.show_pdf_page(page.rect, img_pdf, 0)
        img_pdf.close()

        # Scale text boxes if reference size provided
        text_boxes = page_data.get("text_boxes")
        ref_size = page_data.get("text_ref_size")
        if text_boxes and ref_size:
            sx = pw / ref_size[0]
            sy = ph / ref_size[1]
            text_boxes = [
                {"x": b["x"] * sx, "y": b["y"] * sy,
                 "w": b["w"] * sx, "h": b["h"] * sy, "text": b["text"]}
                for b in text_boxes
            ]

        # Invisible text overlay
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
                if "rect" in link:
                    pending_links.append((page_idx, link["rect"], link["target_page"]))
                elif "from_frac" in link:
                    x0, y0, x1, y1 = link["from_frac"]
                    rect = fitz.Rect(x0 * pw, y0 * ph, x1 * pw, y1 * ph)
                    pending_links.append((page_idx, rect, link["target_page"]))

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
