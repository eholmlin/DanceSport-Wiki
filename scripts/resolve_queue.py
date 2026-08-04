"""Tiny CLI for working the resolution_queue (spec section 5: "Build a tiny
CLI or web view for working the queue.").

Usage:
    python scripts/resolve_queue.py list
    python scripts/resolve_queue.py show <id>
    python scripts/resolve_queue.py decide <id> merge|new_person|rejected
"""
from __future__ import annotations

import click

from dsr.db import get_session
from dsr.models import Person
from dsr.resolve.queue import decide as decide_queue_entry
from dsr.resolve.queue import list_pending


@click.group()
def cli():
    pass


@cli.command("list")
def list_cmd():
    """List all pending resolution_queue entries."""
    session = get_session()
    pending = list_pending(session)
    if not pending:
        click.echo("No pending entries.")
        return
    for entry in pending:
        candidate = session.get(Person, entry.candidate_person_id)
        click.echo(
            f"[{entry.id}] {entry.raw_name!r} (source={entry.source}) "
            f"score={float(entry.score):.3f} candidate={candidate.display_name!r} (person_id={candidate.id})"
        )


@cli.command("show")
@click.argument("queue_id", type=int)
def show_cmd(queue_id: int):
    """Show full context for one resolution_queue entry."""
    session = get_session()
    from dsr.models import ResolutionQueue

    entry = session.get(ResolutionQueue, queue_id)
    if entry is None:
        raise click.ClickException(f"no resolution_queue row with id={queue_id}")
    candidate = session.get(Person, entry.candidate_person_id)
    new_person_id = (entry.context or {}).get("new_person_id")
    new_person = session.get(Person, new_person_id) if new_person_id else None
    click.echo(f"queue id:        {entry.id}")
    click.echo(f"status:          {entry.status}")
    click.echo(f"raw_name:        {entry.raw_name!r}")
    click.echo(f"source:          {entry.source}")
    click.echo(f"score:           {float(entry.score):.3f}")
    click.echo(f"candidate:       {candidate.display_name!r} (person_id={candidate.id})")
    if new_person is not None:
        click.echo(f"new (provisional) person: {new_person.display_name!r} (person_id={new_person.id})")
    click.echo(f"context:         {entry.context}")


@cli.command("decide")
@click.argument("queue_id", type=int)
@click.argument("decision", type=click.Choice(["merge", "new_person", "rejected"]))
def decide_cmd(queue_id: int, decision: str):
    """Apply a human decision: merge the new person into the candidate, or
    confirm it's a genuinely different person (new_person/rejected)."""
    session = get_session()
    entry = decide_queue_entry(session, queue_id, decision)  # type: ignore[arg-type]
    session.commit()
    click.echo(f"queue entry {entry.id} -> {entry.status}")


if __name__ == "__main__":
    cli()
