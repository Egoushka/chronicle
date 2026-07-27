"""Firefly III — money. MariaDB, not Postgres.

Spending is a behavioural signal you do not curate, which makes it more honest
than anything said in a chat. "What was happening before things went wrong" is
often answered better by a transaction pattern than by a conversation.

Two care points:
  * Firefly stores amounts on `transactions` (two rows per journal, signed),
    not on the journal. Take the positive leg or you double-count.
  * The corpus spans 2018-2026 and UAH inflation over that window is large.
    The date is attached to every event precisely so amounts are never
    compared across years without it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

from .base import Density, SourceEvent, SqlAdapter, register


@register
class FireflyAdapter(SqlAdapter):
    source = "firefly"
    dialect = "mariadb"
    density = Density.DISCRETE

    def fetch(self, since: datetime | None = None,
              until: datetime | None = None) -> Iterator[SourceEvent]:
        sql = """
            SELECT j.id, j.date, j.description,
                   t.amount, tc.code AS currency,
                   src.name AS src_account, dst.name AS dst_account,
                   cat.name AS category, tt.type AS ttype
            FROM transaction_journals j
            JOIN transactions t          ON t.transaction_journal_id = j.id AND t.amount > 0
            JOIN transaction_types tt    ON tt.id = j.transaction_type_id
            LEFT JOIN transaction_currencies tc ON tc.id = t.transaction_currency_id
            LEFT JOIN accounts src       ON src.id = (
                SELECT account_id FROM transactions
                WHERE transaction_journal_id = j.id AND amount < 0 LIMIT 1)
            LEFT JOIN accounts dst       ON dst.id = t.account_id
            LEFT JOIN category_transaction_journal ctj ON ctj.transaction_journal_id = j.id
            LEFT JOIN categories cat     ON cat.id = ctj.category_id
            WHERE j.deleted_at IS NULL
              AND (%(since)s IS NULL OR j.date > %(since)s)
              AND (%(until)s IS NULL OR j.date <= %(until)s)
            ORDER BY j.date
        """
        for (jid, date, desc, amount, currency, src_acc, dst_acc,
             category, ttype) in self._stream(sql, {"since": since, "until": until}):
            ts = date if isinstance(date, datetime) else datetime.combine(
                date, datetime.min.time())
            yield SourceEvent(
                source=self.source,
                source_id=str(jid),
                ts=ts,
                text=f"{ttype}: {desc} — {amount} {currency or ''}".strip(),
                actor="me",
                kind="transaction",
                thread_key=f"firefly:{category or 'uncategorized'}",
                payload={"amount": float(amount), "currency": currency,
                         "from": src_acc, "to": dst_acc,
                         "category": category, "type": ttype},
            )
