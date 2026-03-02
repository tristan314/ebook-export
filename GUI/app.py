#!/usr/bin/env python3
"""eBook Export — customtkinter GUI."""

import importlib
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading

# ── Path setup ───────────────────────────────────────────────────────────────
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PARENT_DIR)

# ── Dependency bootstrap ─────────────────────────────────────────────────────
def _auto_install(packages):
    """pip-install a list of packages silently."""
    if not packages:
        return
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", *packages],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

def _ensure_deps():
    """Check all required packages (CLI + GUI) and auto-install missing ones."""
    needed = {
        "customtkinter": "customtkinter",
        "requests": "requests",
        "aiohttp": "aiohttp",
        "fitz": "pymupdf",
        "rich": "rich",
        "keyring": "keyring",
        "cryptography": "cryptography",
    }
    missing = []
    for module, pip_name in needed.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(pip_name)
    if missing:
        print(f"Installing {len(missing)} missing package(s)...")
        _auto_install(missing)

_ensure_deps()

# ── Imports (after deps satisfied) ───────────────────────────────────────────
import customtkinter as ctk
import fitz
from tkinter import filedialog

from config import load_config, save_config, get_credentials, store_credentials, CONFIG_PATH
from platforms import get_platform

DEFAULT_OUTPUT_DIR = os.path.join(PARENT_DIR, "eBooks")


# ── Console redirect ─────────────────────────────────────────────────────────
class QueueWriter(io.TextIOBase):
    """Captures writes to a queue for draining into a GUI textbox."""

    def __init__(self):
        super().__init__()
        self._queue = queue.Queue()

    def write(self, text):
        if text:
            self._queue.put(text)
        return len(text) if text else 0

    def flush(self):
        pass

    def drain(self):
        chunks = []
        while True:
            try:
                chunks.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return "".join(chunks)


# ── Progress bridge ──────────────────────────────────────────────────────────
class GUIProgress:
    """Drop-in replacement for Rich Progress — bridges to GUI progress bars.

    Thread-safe: worker threads call add_task/update, the GUI main thread
    polls get_snapshot() on a timer and updates widgets accordingly.
    """

    def __init__(self):
        self._tasks = {}
        self._next_id = 0
        self._lock = threading.Lock()

    def add_task(self, description, total=100, **kwargs):
        with self._lock:
            tid = self._next_id
            self._next_id += 1
            self._tasks[tid] = {
                "description": re.sub(r"\[.*?\]", "", description).strip(),
                "total": max(total, 1),
                "completed": 0,
                "is_new": True,
            }
            return tid

    def update(self, task_id, advance=0, completed=None, description=None, **kwargs):
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            if completed is not None:
                task["completed"] = completed
            if advance:
                task["completed"] += advance
            if description is not None:
                task["description"] = re.sub(r"\[.*?\]", "", description).strip()

    def get_snapshot(self):
        """Return a copy of all task states and reset is_new flags."""
        with self._lock:
            snap = {}
            for tid, t in self._tasks.items():
                snap[tid] = dict(t)
                t["is_new"] = False
            return snap

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


# ── App ──────────────────────────────────────────────────────────────────────

FONT_LARGE = ("", 20)
FONT_NORMAL = ("", 14)
FONT_SMALL = ("", 12)
PAD = 16
STRIP_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("eBook Export")
        self.geometry("700x580")
        self.minsize(600, 480)

        # State
        self.platform_name = None
        self.platform = None
        self.auth = None
        self.books = []
        self.cfg = load_config()
        self.current_book = None
        self.output_file = None
        self.output_dir = self.cfg.get("output_dir", DEFAULT_OUTPUT_DIR)

        # Console redirect
        self.queue_writer = QueueWriter()

        # Progress bridge state (used during export)
        self.gui_progress = None
        self._progress_bars = {}   # tid → CTkProgressBar
        self._progress_labels = {} # tid → CTkLabel
        self._progress_area = None

        # Container for screen frames
        self.container = ctk.CTkFrame(self, fg_color="transparent")
        self.container.pack(fill="both", expand=True)

        self._show_platform_screen()

    # ── Helpers ──────────────────────────────────────────────────────────

    def _clear(self):
        for w in self.container.winfo_children():
            w.destroy()
        self._progress_bars.clear()
        self._progress_labels.clear()
        self._progress_area = None

    def _run_in_thread(self, fn, args, on_success, on_error):
        def wrapper():
            try:
                result = fn(*args)
                self.after(0, on_success, result)
            except Exception as e:
                self.after(0, on_error, e)
        threading.Thread(target=wrapper, daemon=True).start()

    def _redirect_console(self):
        """Redirect Rich console + progress to GUI widgets."""
        import ui
        ui.console.file = self.queue_writer
        ui.console._force_terminal = False
        # Monkey-patch make_progress to return our bridge
        self.gui_progress = GUIProgress()
        self._orig_make_progress = ui.make_progress
        ui.make_progress = lambda: self.gui_progress

    def _restore_console(self):
        import ui
        ui.console.file = sys.stderr
        if hasattr(self, "_orig_make_progress"):
            ui.make_progress = self._orig_make_progress

    # ── Screen 1: Platform Selection ─────────────────────────────────────

    def _show_platform_screen(self):
        self._clear()
        frame = ctk.CTkFrame(self.container, fg_color="transparent")
        frame.pack(expand=True)

        ctk.CTkLabel(frame, text="eBook Export", font=("", 28, "bold")).pack(pady=(0, 8))
        ctk.CTkLabel(frame, text="Choose a platform", font=FONT_NORMAL,
                     text_color="gray").pack(pady=(0, 32))

        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.pack()

        for name in ("klett", "cornelsen"):
            display = "Klett" if name == "klett" else "Cornelsen"
            btn = ctk.CTkButton(
                btn_frame, text=display, font=FONT_LARGE,
                width=200, height=60, corner_radius=12,
                command=lambda n=name: self._select_platform(n),
            )
            btn.pack(side="left", padx=12)

    def _select_platform(self, name):
        self.platform_name = name
        self.platform = get_platform(name)
        self.cfg = load_config()
        self._show_login_screen()

    # ── Screen 2: Login + Settings ───────────────────────────────────────

    def _show_login_screen(self):
        self._clear()
        display = self.platform_name.title()

        frame = ctk.CTkFrame(self.container, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=PAD * 2, pady=PAD)

        # Header
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 16))
        ctk.CTkButton(header, text="< Back", width=70, font=FONT_SMALL,
                       fg_color="transparent", hover_color=("gray85", "gray25"),
                       text_color=("gray30", "gray70"),
                       command=self._show_platform_screen).pack(side="left")
        ctk.CTkLabel(header, text=f"{display} — Login", font=FONT_LARGE
                     ).pack(side="left", padx=12)

        # Credentials
        email_saved, pw_saved = get_credentials(self.platform_name)

        ctk.CTkLabel(frame, text="Email", font=FONT_NORMAL, anchor="w").pack(fill="x")
        self.email_entry = ctk.CTkEntry(frame, font=FONT_NORMAL, height=36)
        self.email_entry.pack(fill="x", pady=(2, 10))
        if email_saved:
            self.email_entry.insert(0, email_saved)

        ctk.CTkLabel(frame, text="Password", font=FONT_NORMAL, anchor="w").pack(fill="x")
        self.pw_entry = ctk.CTkEntry(frame, font=FONT_NORMAL, height=36, show="*")
        self.pw_entry.pack(fill="x", pady=(2, 16))
        if pw_saved:
            self.pw_entry.insert(0, pw_saved)

        # Platform-specific settings
        settings_frame = ctk.CTkFrame(frame, fg_color="transparent")
        settings_frame.pack(fill="x", pady=(0, 8))

        if self.platform_name == "klett":
            ctk.CTkLabel(settings_frame, text="Image Scale (1-4)", font=FONT_SMALL,
                         anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 8))
            self.scale_var = ctk.StringVar(value=str(self.cfg.get("scale", 4)))
            ctk.CTkOptionMenu(settings_frame, variable=self.scale_var,
                              values=["1", "2", "3", "4"], width=80,
                              font=FONT_SMALL).grid(row=0, column=1, padx=(0, 20))

        elif self.platform_name == "cornelsen":
            ctk.CTkLabel(settings_frame, text="Quality (1-6)", font=FONT_SMALL,
                         anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 8))
            self.quality_var = ctk.StringVar(value=str(self.cfg.get("quality", 4)))
            ctk.CTkOptionMenu(settings_frame, variable=self.quality_var,
                              values=["1", "2", "3", "4", "5", "6"], width=80,
                              font=FONT_SMALL).grid(row=0, column=1, padx=(0, 20))

            ctk.CTkLabel(settings_frame, text="Method", font=FONT_SMALL,
                         anchor="w").grid(row=0, column=2, sticky="w", padx=(0, 8))
            self.method_var = ctk.StringVar(value=self.cfg.get("method", "auto"))
            ctk.CTkOptionMenu(settings_frame, variable=self.method_var,
                              values=["auto", "lossless", "tiles"], width=110,
                              font=FONT_SMALL).grid(row=0, column=3)

        ctk.CTkLabel(settings_frame, text="Max Downloads", font=FONT_SMALL,
                     anchor="w").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        self.concurrency_var = ctk.StringVar(
            value=str(self.cfg.get("max_concurrent_downloads", 10)))
        ctk.CTkOptionMenu(settings_frame, variable=self.concurrency_var,
                          values=["1", "2", "5", "10", "15", "20"], width=80,
                          font=FONT_SMALL).grid(row=1, column=1, padx=(0, 20), pady=(8, 0))

        # Output directory
        ctk.CTkLabel(frame, text="Download to", font=FONT_NORMAL, anchor="w"
                     ).pack(fill="x", pady=(8, 0))
        dir_frame = ctk.CTkFrame(frame, fg_color="transparent")
        dir_frame.pack(fill="x", pady=(2, 12))

        self.output_dir_var = ctk.StringVar(value=self.output_dir)
        dir_entry = ctk.CTkEntry(dir_frame, textvariable=self.output_dir_var,
                                 font=FONT_SMALL, height=32)
        dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(dir_frame, text="Browse...", width=80, height=32,
                       font=FONT_SMALL, command=self._browse_output_dir
                       ).pack(side="right")

        # Login button + status
        self.login_btn = ctk.CTkButton(frame, text="Login", font=FONT_NORMAL,
                                       height=40, command=self._do_login)
        self.login_btn.pack(fill="x", pady=(8, 6))

        self.status_label = ctk.CTkLabel(frame, text="", font=FONT_SMALL,
                                         text_color="gray")
        self.status_label.pack()

    def _browse_output_dir(self):
        path = filedialog.askdirectory(
            initialdir=self.output_dir_var.get(),
            title="Choose download folder",
        )
        if path:
            self.output_dir_var.set(path)

    def _save_output_dir(self, path):
        """Persist output_dir to config.json (save_config filters it out)."""
        self.output_dir = path
        data = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                data = json.load(f)
        data["output_dir"] = path
        with open(CONFIG_PATH, "w") as f:
            json.dump(data, f, indent=2)

    def _save_settings(self):
        email = self.email_entry.get().strip()
        password = self.pw_entry.get().strip()

        if not email or not password:
            return None, None

        if self.platform_name == "klett":
            self.cfg["scale"] = int(self.scale_var.get())
        elif self.platform_name == "cornelsen":
            self.cfg["quality"] = int(self.quality_var.get())
            self.cfg["method"] = self.method_var.get()

        self.cfg["max_concurrent_downloads"] = int(self.concurrency_var.get())
        self.cfg[f"email_{self.platform_name}"] = email

        save_config(self.cfg)
        store_credentials(self.platform_name, email, password)

        # Persist output_dir separately (save_config would filter it out)
        self._save_output_dir(self.output_dir_var.get().strip() or DEFAULT_OUTPUT_DIR)

        return email, password

    def _do_login(self):
        email, password = self._save_settings()
        if not email:
            self.status_label.configure(text="Please enter email and password.",
                                        text_color="#e55")
            return

        self.login_btn.configure(state="disabled", text="Logging in...")
        self.status_label.configure(text="Authenticating...", text_color="gray")

        def authenticate():
            auth = self.platform.authenticate(email, password)
            books = self.platform.fetch_library(auth)
            return auth, books

        def on_success(result):
            self.auth, self.books = result
            self.login_btn.configure(state="normal", text="Login")
            self._show_library_screen()

        def on_error(e):
            self.login_btn.configure(state="normal", text="Login")
            self.status_label.configure(text=str(e), text_color="#e55")

        self._run_in_thread(authenticate, (), on_success, on_error)

    # ── Screen 3: Library ────────────────────────────────────────────────

    def _show_library_screen(self):
        self._clear()
        display = self.platform_name.title()

        frame = ctk.CTkFrame(self.container, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=PAD, pady=PAD)

        # Header
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 12))

        ctk.CTkButton(header, text="< Back", width=70, font=FONT_SMALL,
                       fg_color="transparent", hover_color=("gray85", "gray25"),
                       text_color=("gray30", "gray70"),
                       command=self._show_platform_screen).pack(side="left")
        ctk.CTkLabel(header, text=f"{display} — Library ({len(self.books)} books)",
                     font=FONT_LARGE).pack(side="left", padx=12)
        ctk.CTkButton(header, text="Settings", width=80, font=FONT_SMALL,
                       command=self._show_login_screen).pack(side="right")

        # Scrollable book list
        scroll = ctk.CTkScrollableFrame(frame)
        scroll.pack(fill="both", expand=True)

        hdr = ctk.CTkFrame(scroll, fg_color="transparent")
        hdr.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(hdr, text="Title", font=("", 12, "bold"), anchor="w").pack(
            side="left", fill="x", expand=True)
        ctk.CTkLabel(hdr, text="", width=80).pack(side="right")

        for book in self.books:
            row = ctk.CTkFrame(scroll, fg_color=("gray92", "gray17"), corner_radius=8)
            row.pack(fill="x", pady=2, ipady=6)

            text_frame = ctk.CTkFrame(row, fg_color="transparent")
            text_frame.pack(side="left", fill="x", expand=True, padx=10)

            ctk.CTkLabel(text_frame, text=book["title"], font=FONT_NORMAL,
                         anchor="w").pack(fill="x")
            if book.get("subtitle"):
                ctk.CTkLabel(text_frame, text=book["subtitle"], font=FONT_SMALL,
                             text_color="gray", anchor="w").pack(fill="x")

            ctk.CTkButton(
                row, text="Export", width=70, height=30, font=FONT_SMALL,
                command=lambda b=book: self._start_export(b),
            ).pack(side="right", padx=10)

    # ── Screen 4: Exporting ──────────────────────────────────────────────

    def _start_export(self, book):
        self.current_book = book
        self._clear()

        frame = ctk.CTkFrame(self.container, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=PAD * 2, pady=PAD)

        ctk.CTkLabel(frame, text=book["title"], font=FONT_LARGE,
                     wraplength=500).pack(pady=(0, 4))
        if book.get("subtitle"):
            ctk.CTkLabel(frame, text=book["subtitle"], font=FONT_SMALL,
                         text_color="gray").pack(pady=(0, 8))

        self.phase_label = ctk.CTkLabel(frame, text="Starting export...",
                                        font=FONT_NORMAL, text_color="gray")
        self.phase_label.pack(pady=(4, 12))

        # Dynamic progress bar area — rows added as tasks arrive
        self._progress_area = ctk.CTkFrame(frame, fg_color="transparent")
        self._progress_area.pack(fill="x", pady=(0, 12))

        # Log area
        self.log_box = ctk.CTkTextbox(frame, font=("Menlo", 11), state="disabled",
                                       wrap="word")
        self.log_box.pack(fill="both", expand=True, pady=(0, 8))

        # Back button (shown on error)
        self.export_back_btn = ctk.CTkButton(
            frame, text="Back to Library", font=FONT_NORMAL,
            command=self._show_library_screen)

        # Redirect console + progress, start polling
        self._redirect_console()
        self._poll_export()

        # Compute expected output path
        book_name = re.sub(r'[<>:"/\\|?*]', '_', book["title"])
        ebooks_dir = os.path.join(PARENT_DIR, "eBooks")
        self.output_file = os.path.join(ebooks_dir, f"{book_name}.pdf")

        self._run_in_thread(
            self.platform.export_book,
            (book, self.auth, self.cfg),
            on_success=lambda _: self._export_finished(),
            on_error=self._export_error,
        )

    def _poll_export(self):
        """Single 50ms timer that updates both progress bars and log text."""
        # ── Progress bars ──
        if self.gui_progress is not None:
            snap = self.gui_progress.get_snapshot()
            for tid, task in snap.items():
                # Create widget row on first sight
                if tid not in self._progress_bars and self._progress_area is not None:
                    row = ctk.CTkFrame(self._progress_area, fg_color="transparent")
                    row.pack(fill="x", pady=(0, 6))

                    lbl = ctk.CTkLabel(row, text=task["description"],
                                       font=FONT_SMALL, anchor="w")
                    lbl.pack(fill="x")

                    bar_row = ctk.CTkFrame(row, fg_color="transparent")
                    bar_row.pack(fill="x")

                    bar = ctk.CTkProgressBar(bar_row, height=12, mode="determinate")
                    bar.set(0)
                    bar.pack(side="left", fill="x", expand=True, padx=(0, 8))

                    count = ctk.CTkLabel(bar_row, text="0 / 0", font=("Menlo", 11),
                                         text_color="gray", width=90, anchor="e")
                    count.pack(side="right")

                    self._progress_bars[tid] = bar
                    self._progress_labels[tid] = (lbl, count)

                # Update existing widgets
                if tid in self._progress_bars:
                    frac = task["completed"] / task["total"]
                    self._progress_bars[tid].set(min(frac, 1.0))
                    lbl, count = self._progress_labels[tid]
                    lbl.configure(text=task["description"])
                    count.configure(text=f"{int(task['completed'])} / {int(task['total'])}")

                    # Update phase label to latest active task
                    if frac < 1.0:
                        self.phase_label.configure(text=task["description"])
                    elif frac >= 1.0:
                        self.phase_label.configure(text=task["description"])

        # ── Log text ──
        text = self.queue_writer.drain()
        if text:
            text = STRIP_ANSI.sub("", text)
            self.log_box.configure(state="normal")
            self.log_box.insert("end", text)
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        # Keep polling while export screen is active
        if hasattr(self, "log_box") and self.log_box.winfo_exists():
            self.after(50, self._poll_export)

    def _export_finished(self):
        self._restore_console()
        self.phase_label.configure(text="Export complete!", text_color="#2a2")

        # Set all bars to full
        for bar in self._progress_bars.values():
            bar.set(1.0)

        # Move PDF to chosen output dir if different from default
        if (self.output_file and os.path.exists(self.output_file)
                and os.path.normpath(self.output_dir) != os.path.normpath(DEFAULT_OUTPUT_DIR)):
            os.makedirs(self.output_dir, exist_ok=True)
            dest = os.path.join(self.output_dir, os.path.basename(self.output_file))
            shutil.move(self.output_file, dest)
            self.output_file = dest

        # Gather file info
        total_pages = 0
        size_mb = 0.0
        if self.output_file and os.path.exists(self.output_file):
            size_mb = os.path.getsize(self.output_file) / (1024 * 1024)
            try:
                doc = fitz.open(self.output_file)
                total_pages = len(doc)
                doc.close()
            except Exception:
                pass

        self.after(800, lambda: self._show_complete_screen(total_pages, size_mb))

    def _export_error(self, e):
        self._restore_console()
        self.phase_label.configure(text=f"Export failed: {e}", text_color="#e55")

        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"\n--- ERROR ---\n{e}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

        self.export_back_btn.pack(pady=(8, 0))

    # ── Screen 5: Complete ───────────────────────────────────────────────

    def _show_complete_screen(self, total_pages, size_mb):
        self._clear()

        frame = ctk.CTkFrame(self.container, fg_color="transparent")
        frame.pack(expand=True)

        ctk.CTkLabel(frame, text="Export Complete", font=("", 26, "bold"),
                     text_color="#2a2").pack(pady=(0, 20))

        card = ctk.CTkFrame(frame, corner_radius=12)
        card.pack(padx=20, pady=(0, 24), ipadx=20, ipady=16)

        ctk.CTkLabel(card, text=self.current_book["title"], font=FONT_NORMAL,
                     wraplength=500).pack(pady=(0, 8))

        details = f"{total_pages} pages  ·  {size_mb:.1f} MB"
        ctk.CTkLabel(card, text=details, font=FONT_SMALL, text_color="gray").pack()

        if self.output_file:
            ctk.CTkLabel(card, text=self.output_file, font=("Menlo", 11),
                         text_color="gray", wraplength=500).pack(pady=(8, 0))

        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.pack()

        if self.output_file and os.path.exists(self.output_file):
            ctk.CTkButton(
                btn_frame, text="Show in Finder", font=FONT_NORMAL,
                width=160, height=40, corner_radius=10,
                command=lambda: subprocess.Popen(["open", "-R", self.output_file]),
            ).pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame, text="Export Another", font=FONT_NORMAL,
            width=160, height=40, corner_radius=10,
            command=self._show_library_screen,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame, text="Quit", font=FONT_NORMAL,
            width=100, height=40, corner_radius=10,
            fg_color=("gray75", "gray30"), hover_color=("gray65", "gray40"),
            command=self.destroy,
        ).pack(side="left", padx=8)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
