import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from coursewatch.config import settings
from coursewatch.db import get_db
from coursewatch.models import Course, Delivery, LoginCode, LoginSession, User, Watch, now

app = FastAPI(title="UMass Course Watch")
DB = Annotated[Session, Depends(get_db)]
COOKIE = "coursewatch_session"


def digest(value: str):
    return hmac.new(settings().auth_secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def normalized_email(value: str):
    email = str(value).lower().strip()
    if email.split("@")[-1] != "umass.edu":
        raise HTTPException(422, "Use your @umass.edu email address")
    return email


@app.middleware("http")
async def check_origin(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.headers.get("origin") != settings().app_origin:
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "Unrecognized request origin"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def current_user(request: Request, db: DB):
    session = db.get(LoginSession, digest(request.cookies.get(COOKIE, "")))
    if not session or session.expires_at <= now():
        raise HTTPException(401, "Please sign in with your UMass email")
    user = db.get(User, session.user_id)
    if not user:
        raise HTTPException(401, "Please sign in")
    return user


AUTH = Annotated[User, Depends(current_user)]


class EmailInput(BaseModel):
    email: EmailStr


class VerifyInput(EmailInput):
    code: str = Field(pattern=r"^\d{6}$")


class WatchInput(BaseModel):
    term: Literal["2027 Spring"] = "2027 Spring"
    subject: Literal["COMPSCI"] = "COMPSCI"
    catalog_number: str = Field(pattern=r"^[1-9][0-9]{2}[A-Z]?$", examples=["220"])
    class_number: str = Field(default="", pattern=r"^(?:[0-9]{5})?$")


@app.get("/api/health")
def health(db: DB):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "term": "2027 Spring", "subject": "COMPSCI"}


@app.post("/api/auth/request-code", status_code=202)
def request_code(data: EmailInput, request: Request, db: DB):
    email = normalized_email(data.email)
    # Serialize requests for the same email, including the first request.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:email))"), {"email": email})
    hour_ago = now() - timedelta(hours=1)
    client_hash = digest(request.client.host if request.client else "unknown")
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": "login-client:" + client_hash}
    )
    count = db.scalar(
        select(func.count())
        .select_from(LoginCode)
        .where(
            LoginCode.email == email,
            LoginCode.created_at > hour_ago,
        )
    )
    client_count = db.scalar(
        select(func.count())
        .select_from(LoginCode)
        .where(
            LoginCode.client_hash == client_hash,
            LoginCode.created_at > hour_ago,
        )
    )
    latest = db.scalar(
        select(LoginCode).where(LoginCode.email == email).order_by(LoginCode.id.desc()).limit(1)
    )
    if (
        count >= 5
        or client_count >= 100
        or (latest and latest.created_at > now() - timedelta(seconds=60))
    ):
        raise HTTPException(429, "Please wait before requesting another code")
    for old in db.scalars(select(LoginCode).where(LoginCode.email == email, ~LoginCode.used)):
        old.used = True
    for pending in db.scalars(
        select(Delivery).where(
            Delivery.recipient == email, Delivery.watch_id.is_(None), Delivery.sent_at.is_(None)
        )
    ):
        db.delete(pending)
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(
        LoginCode(
            email=email,
            client_hash=client_hash,
            code_hash=digest(f"{email}:{code}"),
            expires_at=now() + timedelta(minutes=10),
        )
    )
    db.add(
        Delivery(
            watch_id=None,
            event_number=0,
            recipient=email,
            subject="Your Course Watch sign-in code",
            body=f"Your code is {code}. It expires in 10 minutes.\n\n"
            "This verifies access to your UMass email. If you did not request it, "
            "ignore this email.",
        )
    )
    db.commit()
    return {"message": "Check your UMass inbox for a six-digit code"}


@app.post("/api/auth/verify")
def verify(data: VerifyInput, response: Response, db: DB):
    email = normalized_email(data.email)
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:email))"), {"email": email})
    code = db.scalar(
        select(LoginCode)
        .where(LoginCode.email == email)
        .order_by(LoginCode.id.desc())
        .limit(1)
        .with_for_update()
    )
    if not code or code.used or code.expires_at <= now() or code.attempts >= 5:
        raise HTTPException(400, "Code invalid or expired; request a new one")
    code.attempts += 1
    if not hmac.compare_digest(code.code_hash, digest(f"{email}:{data.code}")):
        db.commit()
        raise HTTPException(400, "Code invalid or expired")
    code.used = True
    user = db.scalar(select(User).where(User.email == email))
    if not user:
        user = User(email=email)
        db.add(user)
        db.flush()
    token = secrets.token_urlsafe(32)
    db.add(
        LoginSession(
            token_hash=digest(token), user_id=user.id, expires_at=now() + timedelta(days=30)
        )
    )
    db.commit()
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=30 * 86400,
        secure=settings().environment == "production",
        path="/",
    )
    return {"email": user.email}


@app.get("/api/me")
def me(user: AUTH):
    return {"email": user.email}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, db: DB):
    session = db.get(LoginSession, digest(request.cookies.get(COOKIE, "")))
    if session:
        db.delete(session)
        db.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"message": "Signed out"}


@app.get("/api/watches")
def watches(user: AUTH, db: DB):
    rows = db.execute(
        select(Watch, Course)
        .join(Course, Course.id == Watch.course_id)
        .where(Watch.user_id == user.id)
        .order_by(Watch.id.desc())
    ).all()
    return [
        {
            "id": w.id,
            "term": c.term,
            "subject": c.subject,
            "catalog_number": c.catalog_number,
            "class_number": w.class_number,
            "state": c.state,
            "detail": c.detail,
            "checked_at": c.checked_at,
            "observed_at": c.observed_at,
            "snapshot": c.snapshot,
            "fresh": c.state == "observed"
            and c.observed_at is not None
            and c.observed_at > now() - timedelta(seconds=settings().poll_interval_seconds * 2),
        }
        for w, c in rows
    ]


@app.post("/api/watches", status_code=201)
def add_watch(data: WatchInput, user: AUTH, db: DB):
    db.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": user.id})
    count = db.scalar(select(func.count()).select_from(Watch).where(Watch.user_id == user.id))
    if count >= 20:
        raise HTTPException(422, "You can watch up to 20 sections or courses")
    key = f"{data.term}:{data.subject}:{data.catalog_number}"
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
    course = db.scalar(
        select(Course).where(
            Course.term == data.term,
            Course.subject == data.subject,
            Course.catalog_number == data.catalog_number,
        )
    )
    if not course:
        course = Course(term=data.term, subject=data.subject, catalog_number=data.catalog_number)
        db.add(course)
        db.flush()
    existing = db.scalar(
        select(Watch).where(
            Watch.user_id == user.id,
            Watch.course_id == course.id,
            Watch.class_number == data.class_number,
        )
    )
    if existing:
        raise HTTPException(409, "You are already watching that course or section")
    watch = Watch(user_id=user.id, course_id=course.id, class_number=data.class_number)
    db.add(watch)
    db.commit()
    return {"id": watch.id}


@app.delete("/api/watches/{watch_id}", status_code=204)
def delete_watch(watch_id: int, user: AUTH, db: DB):
    watch = db.get(Watch, watch_id)
    if not watch or watch.user_id != user.id:
        raise HTTPException(404, "Watch not found")
    for delivery in db.scalars(
        select(Delivery).where(Delivery.watch_id == watch_id, Delivery.sent_at.is_(None))
    ):
        db.delete(delivery)
    db.delete(watch)
    db.commit()
