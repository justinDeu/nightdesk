"""Module-form entry point: ``python -m nightdesk._run_ticket_cli <id>``.

Fallback used by the daemon when the ``nightdesk-run-ticket`` console
script isn't on PATH (e.g. zipped installs, embedded Python). Delegates
to the same ``cli.run_ticket`` function as the console script.
"""
from nightdesk.cli import run_ticket


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    run_ticket()
