"""
The ADR-023 two-zone split, enforced against auth.GRANTS().

`python auth.py` is a convenience: one command, every scope the service
needs. The risk it introduces is precisely that convenience — the next
capability is one line in `GRANTS()`, and the easiest line to write is
the one that appends to the token file already there.

That line would be a silent, total defeat of ADR-003. `token.json` is
held by the scheduled digest and by /chat, both of which put untrusted
email bodies into a model's context (ADR-004). A write scope on that file
is reachable by anyone who can send you mail, and nothing downstream
would fail or even look different.

So the rule lives in a test rather than in a comment.

Run:  python -m pytest tests -q
"""

from __future__ import annotations

import auth

# Google's own naming convention, used here as the tripwire: a read-only
# scope ends in `.readonly`. `gmail.compose`, `calendar.events` and
# `gmail.modify` do not, and none of them may share the read token.
READ_ONLY_SUFFIX = ".readonly"

READ_TOKEN = "token.json"


def test_grants_resolve():
    """A guard on the guard. GRANTS() imports connectors, calendars and
    executor lazily, so a broken import here would surface as an empty
    list and every assertion below would pass while checking nothing."""
    grants = auth.GRANTS()

    assert len(grants) >= 2, f"expected read and write grants, got {grants}"
    assert {g.name for g in grants} >= {"read", "write"}
    for grant in grants:
        assert grant.scopes, f"grant {grant.name!r} declares no scopes"
        assert grant.proves, (
            f"grant {grant.name!r} names no command that exercises it — "
            f"minting a token proves nothing on its own (ADR-026)")


def test_the_read_token_holds_only_readonly_scopes():
    """The invariant. If this fails, read ADR-023 before changing it."""
    read = [g for g in auth.GRANTS() if g.token_file == READ_TOKEN]
    assert read, f"no grant writes {READ_TOKEN}"

    for grant in read:
        for scope in grant.scopes:
            assert scope.endswith(READ_ONLY_SUFFIX), (
                f"{scope} is on {READ_TOKEN}, which the scheduled digest "
                f"and /chat hold while reading untrusted email (ADR-004). "
                f"A scope that can change the world does not belong there. "
                f"Give it its own token file, as ADR-023 did for "
                f"calendar.events."
            )


def test_write_scopes_are_on_their_own_file():
    """The other half: a write scope must not merely be absent from the
    read grant, it must live somewhere else entirely."""
    from executor import CALENDAR_WRITE_SCOPES

    by_file: dict[str, set[str]] = {}
    for grant in auth.GRANTS():
        by_file.setdefault(grant.token_file, set()).update(grant.scopes)

    for scope in CALENDAR_WRITE_SCOPES:
        holders = [f for f, scopes in by_file.items() if scope in scopes]
        assert holders, f"{scope} is not requested by any grant"
        assert READ_TOKEN not in holders, (
            f"{scope} would land on {READ_TOKEN}, defeating ADR-023")


def test_no_token_file_is_shared_by_read_and_write_grants():
    """Two grants may not name the same file if one of them writes. This
    catches the shape where someone adds a THIRD grant pointing back at
    token.json rather than editing the read grant's scope tuple."""
    writes: set[str] = set()
    reads: set[str] = set()
    for grant in auth.GRANTS():
        for scope in grant.scopes:
            (reads if scope.endswith(READ_ONLY_SUFFIX) else writes).add(
                grant.token_file)

    overlap = reads & writes
    assert not overlap, (
        f"token file(s) {sorted(overlap)} carry both read-only and write "
        f"scopes. The zones are separated by FILE (ADR-023); a shared file "
        f"is the split existing in name only."
    )
