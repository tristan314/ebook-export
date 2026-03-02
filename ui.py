"""Rich console helpers."""

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    Progress, SpinnerColumn, BarColumn, TextColumn,
    MofNCompleteColumn, TimeRemainingColumn,
)
from rich.prompt import Prompt
from rich.table import Table
from rich import box

console = Console()

BANNER = r"""
[bold cyan]
    ╔═╗╔╗ ╔═╗╔═╗╦╔═  ╔═╗ ╦ ╔╔═╗╔═╗╦═╗╔╦╗╔═╗╦═╗
    ║╣ ╠╩╗║ ║║ ║╠╩╗  ║╣ ╔╩╦╝╠═╝║ ║╠╦╝ ║ ║╣ ╠╦╝
    ╚═╝╚═╝╚═╝╚═╝╩ ╩  ╚═╝╚ ╚ ╩  ╚═╝╩╚═ ╩ ╚═╝╩╚═
[/bold cyan]"""


def print_header():
    console.print(BANNER)


def print_error(msg):
    console.print(f"[bold red]Error:[/bold red] {msg}")


def print_success(msg):
    console.print(f"[green]{msg}[/green]")


def print_dim(msg):
    console.print(f"[dim]  {msg}[/dim]")


_progress_factory = None


def set_progress_factory(factory):
    """Override the progress object factory (used by the GUI)."""
    global _progress_factory
    _progress_factory = factory


def make_progress():
    if _progress_factory is not None:
        return _progress_factory()
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=40),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=console,
    )


def show_export_complete(output_file, total_pages, size_mb, extra=""):
    detail = f"{total_pages} pages  ·  {size_mb:.1f} MB"
    if extra:
        detail += f"  ·  {extra}"
    console.print()
    console.print(Panel(
        f"[bold green]{output_file}[/bold green]\n[dim]{detail}[/dim]",
        title="[bold]Export Complete[/bold]",
        border_style="green",
        box=box.DOUBLE,
    ))


def select_from_list(items, labels, prompt_text="Select", columns=None):
    """Numbered selection with 'config' and 'quit' support.

    Args:
        items: list of items to choose from
        labels: list of row tuples (for multi-column) or strings
        prompt_text: prompt label
        columns: list of (name, style) tuples for table columns

    Returns:
        selected item, or "config" / "quit" string
    """
    console.print()
    table = Table(box=box.ROUNDED, title="[bold]Your eBook Library[/bold]", title_style="cyan")
    table.add_column("#", style="bold cyan", width=4)

    if columns:
        for name, style in columns:
            table.add_column(name, style=style)
    else:
        table.add_column("Title", style="white")

    for i, label in enumerate(labels, 1):
        if isinstance(label, (list, tuple)):
            table.add_row(str(i), *[str(x) for x in label])
        else:
            table.add_row(str(i), str(label))

    console.print(table)
    console.print("[dim]  Type 'config' to reconfigure, 'quit' to exit[/dim]")
    console.print()

    while True:
        raw = Prompt.ask(f"[bold]{prompt_text}[/bold]").strip().lower()
        if raw in ("config", "quit"):
            return raw
        try:
            choice = int(raw)
            if 1 <= choice <= len(items):
                return items[choice - 1]
        except ValueError:
            pass
        console.print(f"[red]Enter a number between 1 and {len(items)}, 'config', or 'quit'[/red]")


def prompt_with_default(label, default=None):
    return Prompt.ask(label, default=default)
