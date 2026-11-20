"""Payment Request follows Minty's sign-in (Phase 9, owner's S1-S2, 2026-11-19).

The 8-hour self-renewing token is gone: ``POST /api/v1/auth/token/refresh`` no longer exists, the
web keeps the 30-minute hand-off token and hands the person over again on a 401. Every call asks
(at most once a minute per sign-in) whether the hub session behind the token's ``sid`` lives, so
signing out of Minty, or 60 minutes without activity, ends Payment Request too.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest
from django.conf import settings
from django.test import Client
from minty_db.models import Entity, User, UserEntity

SESSION_URL = "/api/v1/auth/session"


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def member(db):
    user = User.objects.create(id="59413a81-db95-5233-928a-14fbdb3fa29b", email="end@minty.com",
                               password="hashed_pw", first_name="End", last_name="Test", username="endtest",
                               system_role="normal")
    entity = Entity.objects.create(id="8f5c6d43-a250-5a1a-910f-957e32b44e27", name="End Test Entity",
                                   country_code="HK", currency_id="11111111-1111-1111-1111-111111111111",
                                   status="disconnected")
    UserEntity.objects.create(user=user, entity=entity, role="admin", approved=True)
    return user, entity


def _token(user_id, entity_id, *, seconds_from_now=1800, **extra):
    return pyjwt.encode({"user_id": str(user_id), "entity_id": str(entity_id), "module": "billing",
                         "exp": datetime.now(UTC) + timedelta(seconds=seconds_from_now),
                         "iat": datetime.now(UTC), **extra}, settings.SECRET_KEY, algorithm="HS256")


def _get(client, token, entity_id):
    return client.get(SESSION_URL, HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_ENTITY_ID=str(entity_id))


def test_the_8_hour_refresh_is_gone(client, member):
    user, entity = member
    resp = client.post("/api/v1/auth/token/refresh", HTTP_AUTHORIZATION=f"Bearer {_token(user.id, entity.id)}",
                       HTTP_X_ENTITY_ID=str(entity.id))
    assert resp.status_code == 404


def test_a_token_whose_sign_in_has_ended_is_401(client, member, hub_sessions):
    user, entity = member
    hub_sessions.end("gone")
    resp = _get(client, _token(user.id, entity.id, sid="gone"), entity.id)
    # The reason (owner, 2026-11-19): the web says "Your session has expired", never renews.
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized", "reason": "session_expired"}


def test_accounts_api_unreachable_refuses_a_sign_in_not_confirmed_lately_with_a_503(client, member, hub_sessions):
    # minty-shared v0.9 (owner, 2026-11-19): a short grace, then refuse - never a sign-out.
    user, entity = member
    hub_sessions.unreachable()
    resp = _get(client, _token(user.id, entity.id, sid="s1"), entity.id)
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Minty can't check your sign-in just now. Try again in a minute."}


def test_an_expired_token_whose_sign_in_has_ended_says_so(client, member, hub_sessions):
    # The usual case after 60 idle minutes: the token ran out AND the sign-in ended.
    user, entity = member
    hub_sessions.end("gone")
    resp = _get(client, _token(user.id, entity.id, sid="gone", seconds_from_now=-3600), entity.id)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized", "reason": "session_expired"}


def test_an_expired_token_whose_sign_in_lives_is_renewed_quietly(client, member, hub_sessions):
    user, entity = member
    resp = _get(client, _token(user.id, entity.id, sid="s1", seconds_from_now=-3600), entity.id)
    assert resp.status_code == 401 and resp.json() == {"detail": "Unauthorized"}
    assert hub_sessions.calls == ["s1"]


def test_a_live_sign_in_is_kept_alive_at_most_once_a_minute(client, member, hub_sessions):
    user, entity = member
    for _ in range(2):
        resp = _get(client, _token(user.id, entity.id, sid="s1"), entity.id)
        assert resp.status_code == 200, resp.content[:300]
    assert hub_sessions.calls == ["s1"]


def test_a_person_level_route_ends_with_the_sign_in_too(client, member, hub_sessions):
    user, entity = member
    hub_sessions.end("gone")
    resp = client.get("/api/v1/auth/entitlements", HTTP_AUTHORIZATION=f"Bearer {_token(user.id, '', sid='gone')}")
    assert resp.status_code == 401 and resp.json()["reason"] == "session_expired"


def test_a_token_from_before_the_sid_is_accepted_until_it_expires(client, member, hub_sessions):
    user, entity = member
    assert _get(client, _token(user.id, entity.id), entity.id).status_code == 200
    assert hub_sessions.calls == []


# Clock skew: accounts-api mints on another host. Every backend allows 60 s (minty_shared.tokens).


def test_a_token_minted_on_a_clock_a_little_ahead_is_accepted(client, member):
    user, entity = member
    ahead = datetime.now(UTC) + timedelta(seconds=30)
    assert _get(client, _token(user.id, entity.id, iat=ahead), entity.id).status_code == 200


def test_a_token_expired_within_the_skew_is_accepted_and_past_it_refused(client, member):
    user, entity = member
    assert _get(client, _token(user.id, entity.id, seconds_from_now=-30), entity.id).status_code == 200
    assert _get(client, _token(user.id, entity.id, seconds_from_now=-90), entity.id).status_code == 401
