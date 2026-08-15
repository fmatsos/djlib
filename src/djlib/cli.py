import typer

app = typer.Typer(no_args_is_help=True, help='Local DJ-library catalogue and deduplication tool.')


@app.callback()
def main() -> None:
    """Local DJ-library catalogue and deduplication tool."""
