"""prompts.render and the enrich prompt split (system = static, user = per call)."""
import pytest

from chronicle import prompts
from chronicle.enrich import DEFAULT_MESSAGES, PROMPT_NAME

#: What the single-message prompt rendered to before it moved to the prompt
#: store, for owner "Yehor", these predicates, this chat/date/text.
OLD_SINGLE_MESSAGE = 'You read one excerpt of Yehor\'s private Telegram chats and extract structured memory. Lines are "sender: text". The sender "me" is Yehor.\n\nReturn ONE JSON object with exactly these keys:\n  "summary":      1-2 sentences, in the excerpt\'s main language, what happened.\n  "topics":       up to 6 short lowercase English topic labels.\n  "importance":   0.0-1.0, how much this would matter to Yehor a year later.\n  "sentiment":    -1.0 (negative) to 1.0 (positive), the overall tone.\n  "facts":        durable facts stated or clearly implied, each\n                  {"subject": person name ("Yehor" for the owner), "predicate": one of\n                  [lives_in, plans], "object": short value, "confidence": 0.0-1.0}.\n                  Only facts about people\'s lives, never about the chat itself.\n  "commitments":  promises to do something later, each {"text": what was\n                  promised, "direction": "i_owe" if Yehor promised,\n                  "owed_to_me" if someone promised Yehor, "due": "YYYY-MM-DD"\n                  or null, "confidence": 0.0-1.0}.\n\nEmpty lists are the right answer for small talk. Do not invent.\n\nChat: C\nDate: 2026-10-08 Thursday\n\nme: hi'

VALUES = dict(owner="Yehor", predicates="lives_in, plans", chat="C",
              date="2026-10-08 Thursday", text="me: hi")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    prompts._cache.clear()
    prompts._logged.clear()
    for k in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_enrich_messages_golden():
    msgs = prompts.render(PROMPT_NAME, DEFAULT_MESSAGES, **VALUES)
    assert [m["role"] for m in msgs] == ["system", "user"]
    # Same words as the old single message, only split before "Chat:".
    assert msgs[0]["content"] + "\n\n" + msgs[1]["content"] == OLD_SINGLE_MESSAGE
    assert msgs[1]["content"] == "Chat: C\nDate: 2026-10-08 Thursday\n\nme: hi"


def test_system_message_is_stable_across_calls():
    a = prompts.render(PROMPT_NAME, DEFAULT_MESSAGES, **VALUES)
    b = prompts.render(PROMPT_NAME, DEFAULT_MESSAGES, **{**VALUES, "chat": "D", "text": "x"})
    assert a[0] == b[0] and a[1] != b[1]


def test_values_are_not_re_expanded():
    msgs = prompts.render(PROMPT_NAME, DEFAULT_MESSAGES, **{**VALUES, "text": "{{owner}} {x}"})
    assert msgs[1]["content"].endswith("{{owner}} {x}")


def test_missing_variable_stays_visible():
    msgs = prompts.render("n", [{"role": "user", "content": "a {{b}}"}])
    assert msgs[0]["content"] == "a {{b}}"


def _configure(monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "http://lf")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")


def test_langfuse_version_is_used_and_logged(monkeypatch, caplog):
    _configure(monkeypatch)
    remote = [{"role": "system", "content": "S {{owner}}"}, {"role": "user", "content": "U {{text}}"}]
    monkeypatch.setattr(prompts, "_fetch", lambda name: (remote, "7"))
    with caplog.at_level("INFO", logger="chronicle.prompts"):
        msgs = prompts.render(PROMPT_NAME, DEFAULT_MESSAGES, **VALUES)
        prompts.render(PROMPT_NAME, DEFAULT_MESSAGES, **VALUES)
    assert [m["content"] for m in msgs] == ["S Yehor", "U me: hi"]
    assert [r.message for r in caplog.records if r.message.startswith("prompt=")] == [
        "prompt=chronicle/enrich@7"]


def test_fetch_failure_falls_back_to_default(monkeypatch):
    _configure(monkeypatch)

    def boom(name):
        raise OSError("down")
    monkeypatch.setattr(prompts, "_fetch", boom)
    assert prompts.get(PROMPT_NAME, DEFAULT_MESSAGES) == (DEFAULT_MESSAGES, "default")


def test_roles_must_match_the_default(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(prompts, "_fetch", lambda n: ([{"role": "user", "content": "x"}], "2"))
    assert prompts.get(PROMPT_NAME, DEFAULT_MESSAGES)[1] == "default"


def test_unconfigured_never_fetches(monkeypatch):
    monkeypatch.setattr(prompts, "_fetch", lambda n: pytest.fail("fetched"))
    assert prompts.get(PROMPT_NAME, DEFAULT_MESSAGES)[1] == "default"
