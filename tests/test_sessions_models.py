"""Unit tests for the in-memory session data structures.

No FastAPI, no network: these are the pure pieces the router and the service
build on. The sliding-window invariant and the metadata merge are the two places
where a quiet bug would corrupt every later turn.
"""

from app.sessions.models import ConversationHistory, ProjectMetadata, Session

# --- ConversationHistory: sliding window ------------------------------------


def test_append_writes_a_user_and_an_assistant_message():
    history = ConversationHistory(max_turns=6)

    history.append(user="hello", assistant="hi there")

    assert [(m.role, m.content) for m in history.messages] == [
        ("user", "hello"),
        ("assistant", "hi there"),
    ]


def test_append_does_not_trim_so_compression_can_see_the_evicted_pairs():
    """Trimming moved to CompressionPolicy: append must keep the full history
    so the policy can route evicted pairs to anchors or the summary."""
    history = ConversationHistory(max_turns=2)

    for i in range(3):
        history.append(user=f"u{i}", assistant=f"a{i}")

    assert len(history.messages) == 6  # 3 turns * (user + assistant), untrimmed
    assert [m.content for m in history.messages] == ["u0", "a0", "u1", "a1", "u2", "a2"]


def test_the_window_helper_drops_the_oldest_pairs_first():
    history = ConversationHistory(max_turns=2)

    for i in range(3):
        history.append(user=f"u{i}", assistant=f"a{i}")
    history._trim()

    assert len(history.messages) == 4  # 2 turns * (user + assistant)
    assert [m.content for m in history.messages] == ["u1", "a1", "u2", "a2"]


def test_trimming_stays_on_a_pair_boundary():
    """Even an odd history is trimmed so roles keep alternating."""
    history = ConversationHistory(max_turns=1)

    # Force an odd, oversized history to prove the trim does not split a pair.
    from app.sessions.models import Message

    history.messages = [
        Message(role="user", content="u0"),
        Message(role="assistant", content="a0"),
        Message(role="user", content="u1"),
    ]
    history._trim()

    assert [(m.role, m.content) for m in history.messages] == [("user", "u1")]


def test_to_messages_returns_llm_ready_dicts_without_a_system_prompt():
    history = ConversationHistory(max_turns=6)
    history.append(user="hello", assistant="hi there")

    messages = history.to_messages()

    assert messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    assert all(m["role"] != "system" for m in messages)


def test_to_messages_prepends_a_regenerated_system_prompt_when_given():
    """The system prompt is not stored; it is passed in fresh and comes first."""
    history = ConversationHistory(max_turns=6)
    history.append(user="hello", assistant="hi there")

    messages = history.to_messages(system_prompt="You are an estimator.")

    assert messages[0] == {"role": "system", "content": "You are an estimator."}
    assert messages[1]["role"] == "user"
    # The prompt is not part of the history itself.
    assert all(m["role"] != "system" for m in history.to_messages())


def test_to_messages_orders_summary_then_anchors_then_window():
    """The composed context order is load-bearing: summary first (oldest
    context), then the anchors (durable commitments), then the live window."""
    from app.sessions.models import Message

    history = ConversationHistory(max_turns=6)
    history.summary = "Earlier we agreed the stack."
    history.anchors = [Message(role="user", content="signed the NDA")]
    history.append(user="add Stripe", assistant="ok")

    messages = history.to_messages()

    assert messages[0]["role"] == "user"
    assert "Earlier conversation summary" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "signed the NDA"}
    assert messages[2] == {"role": "user", "content": "add Stripe"}
    assert messages[3] == {"role": "assistant", "content": "ok"}


def test_a_session_starts_without_a_resolved_tier():
    session = Session()
    assert session.last_resolved_tier is None
    assert session.last_tier_rule is None


# --- ProjectMetadata: emptiness and merge -----------------------------------


def test_a_fresh_metadata_is_empty():
    assert ProjectMetadata().is_empty() is True


def test_metadata_with_any_field_is_not_empty():
    assert ProjectMetadata(project_name="Alpha").is_empty() is False
    assert ProjectMetadata(mentioned_technologies=["FastAPI"]).is_empty() is False
    assert ProjectMetadata(assumed_team_size=3).is_empty() is False


def test_merge_overwrites_scalars_when_the_update_is_set():
    previous = ProjectMetadata(project_name="Alpha", assumed_team_size=2)
    update = ProjectMetadata(project_name="Beta")

    merged = previous.merge_with(update)

    assert merged.project_name == "Beta"  # refined
    assert merged.assumed_team_size == 2  # untouched


def test_merge_keeps_the_previous_scalar_when_the_update_is_empty():
    previous = ProjectMetadata(project_name="Alpha")
    update = ProjectMetadata()

    assert previous.merge_with(update).project_name == "Alpha"


def test_merge_unions_technologies_case_insensitively():
    previous = ProjectMetadata(mentioned_technologies=["FastAPI", "Postgres"])
    update = ProjectMetadata(mentioned_technologies=["fastapi", "Redis"])

    merged = previous.merge_with(update)

    assert merged.mentioned_technologies == ["FastAPI", "Postgres", "Redis"]


# --- Session ----------------------------------------------------------------


def test_each_session_gets_a_distinct_id():
    assert Session().session_id != Session().session_id


def test_a_session_starts_with_empty_history_and_metadata():
    session = Session()

    assert session.history.messages == []
    assert session.metadata.is_empty() is True
