#!/usr/bin/env python3
"""eBook Export Tool — unified exporter for Klett and Cornelsen eBooks."""

# Dependency check must run first (stdlib only)
from deps import check_and_install
check_and_install()

# Now safe to import third-party packages
import sys

import config
from ui import console, print_header, print_error, print_success, select_from_list
from platforms import get_platform, platform_names
from rich.prompt import Prompt


def select_platform():
    """Let user pick Klett or Cornelsen. Returns platform name string."""
    names = platform_names()

    console.print("[bold]Select a platform:[/bold]")
    for i, name in enumerate(names, 1):
        mod = get_platform(name)
        console.print(f"  [cyan]{i}[/cyan]  {mod.DISPLAY_NAME}")
    console.print()

    while True:
        raw = Prompt.ask("[bold]Platform[/bold]").strip().lower()
        try:
            idx = int(raw)
            if 1 <= idx <= len(names):
                return names[idx - 1]
        except ValueError:
            pass
        if raw in names:
            return raw
        console.print(f"[red]Enter a number (1-{len(names)}) or platform name[/red]")


def main():
    print_header()

    cfg = config.load_config()

    # Platform selection
    platform_name = select_platform()
    platform = get_platform(platform_name)
    console.print(f"[bold]{platform.DISPLAY_NAME}[/bold]")
    console.print()

    # Config wizard on first run or missing credentials
    if not config.has_credentials(platform_name):
        cfg = config.run_config_wizard(platform_name, console)

    # Authenticate
    console.print("[bold]Authenticating...[/bold]")
    email, password = config.get_credentials(platform_name)
    if not email or not password:
        print_error("No credentials found. Run config wizard.")
        cfg = config.run_config_wizard(platform_name, console)
        email, password = config.get_credentials(platform_name)

    try:
        auth = platform.authenticate(email, password)
    except Exception as e:
        print_error(f"Authentication failed: {e}")
        sys.exit(1)
    print_success("Authenticated")
    console.print()

    # Fetch library
    console.print("[bold]Loading library...[/bold]")
    try:
        books = platform.fetch_library(auth)
    except Exception as e:
        print_error(f"Failed to load library: {e}")
        sys.exit(1)

    # Export loop
    while True:
        columns = platform.book_list_columns()
        labels = platform.book_labels(books)

        selection = select_from_list(
            books, labels,
            prompt_text="Select a book",
            columns=columns,
        )

        if selection == "quit":
            console.print("[dim]Goodbye![/dim]")
            break
        elif selection == "config":
            cfg = config.run_config_wizard(platform_name, console)
            continue

        # Export selected book
        console.print()
        try:
            platform.export_book(selection, auth, cfg)
        except Exception as e:
            print_error(f"Export failed: {e}")

        console.print()
        again = Prompt.ask("Download another?", choices=["y", "n"], default="y")
        if again.lower() != "y":
            console.print("[dim]Goodbye![/dim]")
            break


if __name__ == "__main__":
    main()
