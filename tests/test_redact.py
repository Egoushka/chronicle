"""Secret redaction — what must go, and the prose that must stay.

Every fake token is assembled at runtime: written out literally, it would
trip gitleaks and the repository's own pre-commit denylist, which is the
point of both.
"""

import pytest

from chronicle.redact import redact, redact_obj

ALNUM = "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6kL9"
TOKENS = {
    "github_token": "gh" + "p_" + ALNUM[:36],
    "api_key": "s" + "k-" + "proj-" + ALNUM[:30],
    "aws_key": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
    "slack_token": "xo" + "xb-" + "1234567890-abcdef",
    "telegram_bot_token": "1234567890:" + "AA" + ALNUM[:33],
    "jwt": "ey" + "J" + ALNUM[:12] + ".ey" + "J" + ALNUM[:12] + "." + ALNUM[:16],
}


@pytest.mark.parametrize("kind", sorted(TOKENS))
def test_known_token_formats_are_redacted(kind):
    text, found = redact(f"here you go: {TOKENS[kind]} — keep it safe")

    assert TOKENS[kind] not in text
    assert f"[REDACTED:{kind}]" in text
    assert text.startswith("here you go:") and text.endswith("keep it safe")
    assert found[kind] == 1


def test_private_key_block_goes_whole():
    begin, end = "-----BEGIN OPENSSH " + "PRIVATE KEY-----", "-----END OPENSSH " + "PRIVATE KEY-----"
    text, found = redact(f"key:\n{begin}\nb3BlbnNzaC1rZXk\nAAAA\n{end}\nthanks")

    assert text == "key:\n[REDACTED:private_key]\nthanks"
    assert found["private_key"] == 1


def test_url_password_keeps_the_host():
    text, _ = redact("db at postgres://app:" + "s3cretpw" + "@db.internal:5432/app")

    assert text == "db at postgres://[REDACTED:url_password]@db.internal:5432/app"


@pytest.mark.parametrize("label", ["password", "Пароль", "пароль", "token", "api_key", "API key",
                                   "DB_PASSWORD"])
def test_labelled_secret_keeps_its_label(label):
    text, found = redact(f"wifi {label}: " + "hunter22x" + " до речі")

    assert "hunter22x" not in text
    assert text == f"wifi {label}: [REDACTED:labelled] до речі"
    assert found["labelled"] == 1


@pytest.mark.parametrize("prose", [
    "token-based auth is fine",           # a dash is not a label separator
    "забув пароль від пошти",              # the word alone, no value
    "the password is weak",               # no `:`/`=`, so no labelled value
    "https://github.com/some/repo",
    "ок",
    "",
])
def test_ordinary_text_is_untouched(prose):
    text, found = redact(prose)

    assert text == prose
    assert not found


@pytest.mark.parametrize("label", ["photos api key", "password manager work", "tracker pass",
                                   "пароль від роутера"])
def test_a_bare_secret_with_its_label_on_another_line(label):
    """The shape karakeep held five times: the value alone on one line, the
    label after it on another. No format pattern sees it."""
    value = "Zq7" + ALNUM[:20] + "#x"
    text, found = redact(f"{value}\n\n{label}")

    assert text == f"[REDACTED:bare_secret]\n\n{label}"
    assert found["bare_secret"] == 1


@pytest.mark.parametrize("text", [
    "commit 3f2a9c1b7e5d4a6f8b0c2e4d6f8a0b2c4d6e8f0a fixed it",   # no label: a hash
    "the key point is https://example.com/a1b2c3d4e5f6g7h8",    # a URL, never a secret
    "api key docs: see ChatGPT4 and Claude35Sonnet",             # labels, but short tokens
    # Inline, not alone on a line: lastfm's JSON and pasted code, from the audit.
    'track key {"mbid": "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d"}',
    "-- foreign key\nint VeryLongIdentifierName2024 = 1;",
    '<!-- key -->\nxmlns:d="http://schemas.microsoft.com/expression/blend/2008"',
    "primary key\nEncoding.GetEncoding(1251).GetString(bytes);",
])
def test_long_tokens_without_a_label_or_that_are_urls_stay(text):
    assert redact(text) == (text, {})


def test_a_value_is_redacted_once_not_twice():
    """`api_key: sk-...` matches both the token format and the label. One
    redaction, counted once, under the specific kind."""
    text, found = redact("api_key: " + TOKENS["api_key"])

    assert text == "api_key: [REDACTED:api_key]"
    assert sum(found.values()) == 1


def test_redaction_is_idempotent():
    """Ingest re-reads an overlap window every run; a second redaction that
    changed anything would rewrite the event and re-embed its segment
    forever. `[REDACTED:url_password]` once matched the user:pass shape."""
    samples = [f"x {t} y" for t in TOKENS.values()] + [
        "postgres://app:" + "s3cretpw" + "@db:5432", "пароль: " + "hunter22x"]
    for s in samples:
        once, _ = redact(s)
        twice, found = redact(once)
        assert twice == once and not found, s


def test_none_passes_through():
    assert redact(None) == (None, {})


def test_payload_strings_are_redacted_recursively():
    payload = {"url": "https://u:" + "pw123456" + "@host/x", "n": 3,
               "tags": ["plain", "token=" + "abcd1234"], "nested": {"ok": None}}

    out, found = redact_obj(payload)

    assert out["url"] == "https://[REDACTED:url_password]@host/x"
    assert out["tags"] == ["plain", "token=[REDACTED:labelled]"]
    assert out["n"] == 3 and out["nested"] == {"ok": None}
    assert found == {"url_password": 1, "labelled": 1}


def test_ingest_redacts_before_the_row_exists():
    """The only call site that matters: `_write_events` builds its rows from
    redacted text and payload. A regression here puts secrets straight back
    into the index, the MCP and the enrich prompt."""
    import inspect

    from chronicle import worker

    src = inspect.getsource(worker._write_events)
    assert "redact(e.text)" in src
    assert "redact_obj(e.payload)" in src
