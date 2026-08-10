"""Forgejo — commits. What you actually built, with messages you wrote.

Postgres 17. Commit messages are deliberate human text, so this is NARRATIVE
despite living in a code forge — and unlike wakapi (which knows you typed) it
knows what you *finished*.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Iterator

from .base import Density, SourceEvent, SqlAdapter, register

log = logging.getLogger(__name__)

#: `action.op_type` is an integer, and it is the only description an action
#: has when `content` is empty (measured: 1 of 87 rows). Only the types this
#: forge actually produces are named; anything else falls back to the number,
#: which is still better than leaking a bare int into indexed text.
OP_TYPES = {
    1: "created repository", 5: "pushed", 7: "opened pull request",
    9: "pushed tag", 10: "commented", 11: "merged pull request",
    12: "closed issue", 24: "published release",
}


def _describe(content: str | None, op_type: int) -> str:
    """Pull the human sentence out of an action row.

    For a push (op_type 5) `content` is a JSON envelope, not prose:
    {"Commits":[{"Sha1":"bdd7b53…","Message":"fix(listmonk): …","AuthorEmail":…}]}
    forgejo is declared NARRATIVE because *commit messages* are deliberate
    text — indexing the envelope instead would embed SHA1 hashes and author
    emails and bury the one sentence that carries meaning.
    """
    if content:
        try:
            commits = json.loads(content).get("Commits") or []
        except (ValueError, AttributeError):
            return content          # not JSON: an issue title, a comment
        msgs = [c["Message"].strip() for c in commits if c.get("Message")]
        if msgs:
            return "; ".join(msgs)
    return OP_TYPES.get(op_type, f"action {op_type}")


@register
class ForgejoAdapter(SqlAdapter):
    source = "forgejo"
    dialect = "postgres"
    density = Density.NARRATIVE

    def __init__(self, dsn: str, author_email: str | None = None):
        super().__init__(dsn)
        self.author_email = author_email

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        # Forgejo keeps commits in git, not SQL; `action` is the durable
        # activity feed and is what you can actually query.
        #
        # Every bound is cast explicitly. A bare `%(since)s` arrives as an
        # untyped NULL when it is None, and Postgres then plans the statement
        # with parameter type `unknown`; the next execution with a real epoch
        # fails with `type of parameter 3 (bigint) does not match that when
        # preparing the plan (unknown)`. The cast fixes the type at plan time.
        sql = """
            SELECT a.id, a.created_unix, a.op_type, a.content,
                   r.name AS repo, u.email
            FROM action a
            JOIN repository r ON r.id = a.repo_id
            JOIN "user" u     ON u.id = a.act_user_id
            WHERE NOT a.is_deleted
              AND (%(email)s::text IS NULL OR u.email = %(email)s::text)
              AND (%(since)s::bigint IS NULL OR a.created_unix > %(since)s::bigint)
              AND (%(until)s::bigint IS NULL OR a.created_unix <= %(until)s::bigint)
            ORDER BY a.created_unix, a.id
        """
        params = {
            "email": self.author_email,
            "since": int(since.timestamp()) if since else None,
            "until": int(until.timestamp()) if until else None,
        }
        for aid, created, op_type, content, repo, email in self._stream(sql, params):
            yield SourceEvent(
                source=self.source, source_id=str(aid),
                ts=datetime.fromtimestamp(created, tz=timezone.utc),
                text=f"[{repo}] {_describe(content, op_type)}",
                actor=email, kind="repo_activity",
                thread_key=f"forgejo:{repo}",
                payload={"repo": repo, "op_type": op_type},
            )
