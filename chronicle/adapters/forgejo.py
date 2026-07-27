"""Forgejo — commits. What you actually built, with messages you wrote.

Postgres 17. Commit messages are deliberate human text, so this is NARRATIVE
despite living in a code forge — and unlike wakapi (which knows you typed) it
knows what you *finished*.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

from .base import Density, SourceEvent, SqlAdapter, register


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
        sql = """
            SELECT a.id, a.created_unix, a.op_type, a.content,
                   r.name AS repo, u.email
            FROM action a
            JOIN repository r ON r.id = a.repo_id
            JOIN "user" u     ON u.id = a.act_user_id
            WHERE (%(email)s IS NULL OR u.email = %(email)s)
              AND (%(since)s IS NULL OR a.created_unix > %(since)s)
              AND (%(until)s IS NULL OR a.created_unix <= %(until)s)
            ORDER BY a.created_unix
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
                text=f"[{repo}] {content or op_type}",
                actor=email, kind="repo_activity",
                thread_key=f"forgejo:{repo}",
                payload={"repo": repo, "op_type": op_type},
            )
