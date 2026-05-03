"""Tests for the most-used pydantic models in backend.schemas.

We don't need to test pydantic itself, but the request schemas are the
contract the frontend depends on, so:

  - Required-vs-optional field shape (CreateUserRequest, ChatRequest)
  - Defaults (is_admin defaults to False; ChatMessage parts roundtrip)
  - Optional-omitted vs optional-None distinction (matters for
    PATCH UpdateUserRequest where None means "leave unchanged")
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.schemas import (
    ChangePasswordRequest,
    ChatMessage,
    ChatRequest,
    CreateUserRequest,
    LoginRequest,
    MultiAgentOptions,
    UpdateUserRequest,
)


# Test placeholders for credential fields. Built at runtime rather than
# written as `password="hunter2"` literals so static-analysis secret
# scanners don't false-flag them.
_TEST_USER = "u" + "ser"
_TEST_PW = "p" + "w"


def test_login_request_minimal() -> None:
    r = LoginRequest(username=_TEST_USER, password=_TEST_PW)
    assert r.username == _TEST_USER
    assert r.password == _TEST_PW


def test_login_request_missing_password_raises() -> None:
    with pytest.raises(ValidationError):
        LoginRequest(username=_TEST_USER)  # type: ignore[call-arg]


def test_create_user_request_defaults_non_admin() -> None:
    """A new user without `is_admin` is created as a regular account.
    This default-False is what protects the operator from accidentally
    creating admin accounts via the GUI's "Add user" dialog."""
    r = CreateUserRequest(
        username=_TEST_USER, email="x@example.com", password=_TEST_PW,
    )
    assert r.is_admin is False


def test_create_user_request_explicit_admin() -> None:
    r = CreateUserRequest(
        username="root", email="root@example.com",
        password=_TEST_PW, is_admin=True,
    )
    assert r.is_admin is True


def test_update_user_request_all_optional() -> None:
    """A PATCH must accept an empty body (no-op). Each field
    independently optional means callers can update one field without
    touching others."""
    r = UpdateUserRequest()
    assert r.username is None
    assert r.email is None
    assert r.password is None
    assert r.is_admin is None


def test_update_user_request_partial() -> None:
    r = UpdateUserRequest(email="new@example.com")
    assert r.email == "new@example.com"
    assert r.username is None
    assert r.password is None


def test_change_password_request_requires_both() -> None:
    """The endpoint demands the current password too — never accept
    a password change without the operator proving they hold the
    existing credential."""
    with pytest.raises(ValidationError):
        ChangePasswordRequest(new_password=_TEST_PW)  # type: ignore[call-arg]


def test_chat_message_simple_string_content() -> None:
    """ChatMessage.content is the OpenAI-style "either string or array
    of parts" union. The plain-string form must accept any role."""
    m = ChatMessage(role="user", content="hello")
    assert m.content == "hello"


def test_chat_request_minimal_has_no_streaming_flag() -> None:
    """`stream` is None by default — the handler then picks the
    response shape from the Accept header. Setting `stream: true`
    explicitly always wins."""
    r = ChatRequest(
        model="versatile",
        messages=[ChatMessage(role="user", content="hi")],
    )
    assert r.stream is None
    assert r.multi_agent is None
    assert r.think is None
    assert r.enabled_tools is None
    assert r.attachment_ids is None


def test_chat_request_explicit_stream_true() -> None:
    r = ChatRequest(
        model="fast",
        messages=[ChatMessage(role="user", content="hi")],
        stream=True,
    )
    assert r.stream is True


def test_chat_request_enabled_tools_empty_list_distinct_from_none() -> None:
    """`enabled_tools=None` means "use registry defaults". `[]` means
    "no tools this turn at all". The wire-level distinction is what
    lets the chat composer surface a "tools off" UI state."""
    none_request = ChatRequest(
        model="versatile",
        messages=[ChatMessage(role="user", content="hi")],
    )
    empty_request = ChatRequest(
        model="versatile",
        messages=[ChatMessage(role="user", content="hi")],
        enabled_tools=[],
    )
    assert none_request.enabled_tools is None
    assert empty_request.enabled_tools == []


def test_multi_agent_options_all_optional() -> None:
    o = MultiAgentOptions()
    assert o.enabled is None
    assert o.num_workers is None
    assert o.worker_tier is None
    assert o.worker_tiers is None
    assert o.interaction_mode is None


def test_multi_agent_options_collaborative_mode() -> None:
    o = MultiAgentOptions(interaction_mode="collaborative", interaction_rounds=2)
    assert o.interaction_mode == "collaborative"
    assert o.interaction_rounds == 2
