import re
from datetime import timedelta

from coursewatch.api import COOKIE, app, digest
from coursewatch.models import Delivery, LoginCode, LoginSession, User, now
from fastapi.testclient import TestClient
from sqlalchemy import select


def sign_in(client, db, email="student@umass.edu"):
    assert client.post("/api/auth/request-code", json={"email": email}).status_code == 202
    body = db.scalar(
        select(Delivery).where(Delivery.recipient == email.lower()).order_by(Delivery.id.desc())
    )
    code = re.search(r"\b\d{6}\b", body.body)[0]
    result = client.post("/api/auth/verify", json={"email": email, "code": code})
    assert result.status_code == 200
    return code


def test_umass_email_only(client):
    for email in ["student@gmail.com", "student@umass.edu.evil.com", "student@cs.umass.edu"]:
        assert client.post("/api/auth/request-code", json={"email": email}).status_code == 422


def test_email_normalized_and_session_revoked(client, db):
    sign_in(client, db, "Student@UMASS.EDU")
    assert client.get("/api/me").json()["email"] == "student@umass.edu"
    token = client.cookies.get(COOKIE)
    assert db.get(LoginSession, digest(token)) is not None
    assert client.post("/api/auth/logout").status_code == 200
    assert db.get(LoginSession, digest(token)) is None
    assert client.get("/api/me").status_code == 401


def test_code_single_use_and_cookie_flags(client, db):
    code = sign_in(client, db)
    response = client.post("/api/auth/verify", json={"email": "student@umass.edu", "code": code})
    assert response.status_code == 400
    assert client.get("/api/me").status_code == 200


def test_wrong_code_limited(client, db):
    client.post("/api/auth/request-code", json={"email": "student@umass.edu"})
    queued = db.scalar(select(Delivery))
    correct = re.search(r"\b\d{6}\b", queued.body)[0]
    wrong = "000000" if correct != "000000" else "111111"
    for _ in range(5):
        assert (
            client.post(
                "/api/auth/verify", json={"email": "student@umass.edu", "code": wrong}
            ).status_code
            == 400
        )
    assert (
        client.post(
            "/api/auth/verify", json={"email": "student@umass.edu", "code": correct}
        ).status_code
        == 400
    )
    assert db.scalar(select(User)) is None


def test_expired_code_rejected(client, db):
    client.post("/api/auth/request-code", json={"email": "student@umass.edu"})
    entry = db.scalar(select(LoginCode))
    entry.expires_at = now() - timedelta(seconds=1)
    db.commit()
    correct = re.search(r"\b\d{6}\b", db.scalar(select(Delivery)).body)[0]
    assert (
        client.post(
            "/api/auth/verify", json={"email": "student@umass.edu", "code": correct}
        ).status_code
        == 400
    )


def test_resend_throttled(client):
    assert (
        client.post("/api/auth/request-code", json={"email": "student@umass.edu"}).status_code
        == 202
    )
    assert (
        client.post("/api/auth/request-code", json={"email": "student@umass.edu"}).status_code
        == 429
    )


def test_csrf_origin_enforced(client):
    assert (
        client.post(
            "/api/auth/request-code",
            headers={"Origin": "https://evil.example"},
            json={"email": "student@umass.edu"},
        ).status_code
        == 403
    )


def test_unauthenticated_watchlist_hidden(client):
    assert client.get("/api/watches").status_code == 401
    assert client.post("/api/watches", json={"catalog_number": "220"}).status_code == 401


def test_watchlist_scoped_and_shared_course(client, db):
    sign_in(client, db)
    first = client.post("/api/watches", json={"catalog_number": "220"})
    assert first.status_code == 201
    assert client.post("/api/watches", json={"catalog_number": "220"}).status_code == 409
    assert (
        client.post("/api/watches", json={"catalog_number": "220", "term": "2026 Fall"}).status_code
        == 422
    )
    with TestClient(app, headers={"Origin": "http://localhost:3000"}) as other:
        sign_in(other, db, "another@umass.edu")
        assert other.get("/api/watches").json() == []
        assert other.delete(f"/api/watches/{first.json()['id']}").status_code == 404
        assert other.post("/api/watches", json={"catalog_number": "220"}).status_code == 201
    from coursewatch.models import Course

    assert len(db.scalars(select(Course)).all()) == 1
    assert len(client.get("/api/watches").json()) == 1
    assert client.delete(f"/api/watches/{first.json()['id']}").status_code == 204


def test_watch_limit(client, db):
    sign_in(client, db)
    for number in range(100, 120):
        assert client.post("/api/watches", json={"catalog_number": str(number)}).status_code == 201
    assert client.post("/api/watches", json={"catalog_number": "220"}).status_code == 422
