"""
Token matching shared by every text filter that tolerates typos: the mail
search's fallback tier and the orders list filter.

One tokenizer (Postgres's own 'simple' parser) and one per-token predicate
(literal substring, or pg_trgm word similarity above one threshold), so two
surfaces that both claim to "match what you typed" cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

# Validated against real typos; applied per transaction through
# set_word_similarity_threshold, because the `%>` operator reads a session
# GUC rather than taking the threshold as an argument.
WORD_SIMILARITY_THRESHOLD = 0.6


def ilike_escape(token: str) -> str:
    """Escape ILIKE's own wildcards in raw user input before wrapping it
    in %...% -- a query containing a literal % or _ must match that
    character, not be treated as a pattern."""
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def resolve_lexemes(session: AsyncSession, query: str) -> list[str]:
    """The distinct lexemes Postgres's own 'simple' text search parser
    extracts from the raw query -- the single tokenization every later
    piece of a search (the prefix tsquery, the per-field ILIKE scope, the
    match tier, the snippet) agrees on. Splitting on whitespace in Python
    instead would let matching and the tsvector index quietly disagree on
    what a "token" is; going through Postgres's own parser is also what
    makes the tsquery injection-proof -- raw user text is never
    interpolated into tsquery syntax directly, only tokenized first.
    """
    lexeme_col = func.unnest(func.to_tsvector("simple", query)).table_valued("lexeme").c.lexeme
    result = await session.execute(select(lexeme_col).distinct())
    return [row[0] for row in result.all()]


async def set_word_similarity_threshold(session: AsyncSession) -> None:
    """Scope WORD_SIMILARITY_THRESHOLD to the current transaction (SET
    LOCAL), which is what `fuzzy_token_predicate`'s `%>` reads."""
    await session.execute(
        text(f"SET LOCAL pg_trgm.word_similarity_threshold = {WORD_SIMILARITY_THRESHOLD}")
    )


def fuzzy_token_predicate(token: str, columns: Sequence[Any]) -> Any:
    """A token matches literally in any of `columns`, or -- pg_trgm's
    word-similarity operator, never the word_similarity() function call --
    close enough that a typo doesn't lose the hit.

    The operator form (`column %> token`) is required for a trigram index
    to serve this at all -- verified by EXPLAIN across all three
    spellings: `column %> 'token'` and `'token' <% column` both use the
    index, `word_similarity(token, column) >= threshold` is a sequential
    scan every time despite computing the identical answer. The caller
    runs `set_word_similarity_threshold` first.

    An '@' token is left literal-only: two unrelated addresses sharing a
    domain score *higher* on word similarity than a genuine typo does, so
    there is no threshold that keeps the typo and drops the collision. An
    address is a literal identifier someone is typing exactly, not prose
    worth typo-tolerance over.
    """
    pattern = f"%{ilike_escape(token)}%"
    literal = [column.ilike(pattern) for column in columns]
    if "@" in token:
        return or_(*literal)
    return or_(*literal, *[column.op("%>")(token) for column in columns])
