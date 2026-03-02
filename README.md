# eBook Export

Export eBooks from **Klett** and **Cornelsen** as searchable PDFs with text overlay and internal links.

## Features

- **Klett** — Downloads page images + SVG word boxes from bridge.klett.de, builds a searchable PDF with invisible text layer and internal page links
- **Cornelsen** — Tries lossless encrypted PDF download first (AES-128-CBC), falls back to PSPDFKit tile download with text layer
- **GUI** (macOS .app) — customtkinter interface with login, library browser, and live progress bars
- **CLI** — Rich-based terminal interface with the same functionality
- Credentials stored securely via OS keyring
- Configurable image quality/scale and download concurrency

## Install

### macOS App

Download `eBook Export.dmg` from the [latest release](https://github.com/tristan314/ebook-export/releases/latest), open it, and drag the app to your Applications folder. Python 3.10+ must be installed (e.g. via [Homebrew](https://brew.sh): `brew install python`).

### CLI

```bash
git clone https://github.com/tristan314/ebook-export.git
cd ebook-export
python3 main.py
```

Dependencies are auto-installed on first run:

| Package | Purpose |
|---------|---------|
| requests | HTTP client |
| aiohttp | Async downloads |
| pymupdf | PDF assembly |
| rich | CLI UI |
| keyring | Credential storage |
| cryptography | Cornelsen lossless PDF decryption (optional) |

## Usage

### GUI

1. Select platform (Klett or Cornelsen)
2. Enter credentials and adjust settings (image scale, download concurrency)
3. Browse your library and click **Export** on any book
4. PDFs are saved to the configured output directory (default: `eBooks/`)

### CLI

```
python3 main.py
```

Follow the prompts to select a platform, configure credentials, and choose a book. Type `config` at the book selection to reconfigure settings, or `quit` to exit.

## Building the macOS App

```bash
python3 GUI/build_macos_app.py
```

This creates `eBook Export.app` one directory above the project. To create a distributable `.dmg`:

```bash
hdiutil create -volname "eBook Export" -srcfolder "eBook Export.app" -ov -format UDZO "eBook Export.dmg"
```

## Project Structure

```
├── main.py              # CLI entry point
├── GUI/
│   ├── app.py           # customtkinter GUI
│   ├── build_macos_app.py
│   └── AppIcon.icns
├── platforms/
│   ├── klett.py         # Klett auth, library, export
│   └── cornelsen.py     # Cornelsen auth, library, export
├── config.py            # Settings + keyring credentials
├── downloader.py        # Async concurrent file downloads
├── pdf_builder.py       # PDF assembly with text overlay + links
├── ui.py                # Rich console helpers + progress bars
├── login_form.py        # HTML form parser for login pages
└── deps.py              # Dependency checker (CLI)
```

## License

For personal/educational use only.
