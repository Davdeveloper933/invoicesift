"""Storage for the validation site: visits/events, feedback and waitlist sign-ups.

SQLite by default (a file in ``data/``). Set ``DATABASE_URL`` to a Postgres URL
(e.g. a free Neon database) on hosts whose disk is wiped on every restart.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.engine import Engine

metadata = MetaData()

events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
    Column("visitor_id", String(64), nullable=False, index=True),
    Column("event", String(40), nullable=False, index=True),
    Column("detail", String(200), nullable=False, default=""),
    Column("source", String(100), nullable=False, default=""),
    Column("ip_hash", String(64), nullable=False, default="", index=True),
)

feedback = Table(
    "feedback",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("visitor_id", String(64), nullable=False),
    Column("rating", String(20), nullable=False, default=""),
    Column("monthly_volume", String(40), nullable=False, default=""),
    Column("comment", Text, nullable=False, default=""),
    Column("source", String(100), nullable=False, default=""),
)

signups = Table(
    "signups",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("visitor_id", String(64), nullable=False),
    Column("email", String(254), nullable=False, index=True),
    Column("plan", String(40), nullable=False, default=""),
    Column("monthly_volume", String(40), nullable=False, default=""),
    Column("integrations", String(300), nullable=False, default=""),
    Column("comment", Text, nullable=False, default=""),
    Column("source", String(100), nullable=False, default=""),
)


# Browsers of the site owner (anyone who logged into /admin). Excluded from all stats.
owners = Table(
    "owners",
    metadata,
    Column("visitor_id", String(64), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
_owner_ids = select(owners.c.visitor_id)
NOT_OWNER_EV = events.c.visitor_id.not_in(_owner_ids)
NOT_OWNER_FB = feedback.c.visitor_id.not_in(_owner_ids)
NOT_OWNER_SU = signups.c.visitor_id.not_in(_owner_ids)


def now() -> datetime:
    return datetime.now(timezone.utc)


def make_engine(url: str | None = None) -> Engine:
    url = url or os.getenv("DATABASE_URL", "")
    if not url:
        data_dir = Path(__file__).resolve().parent.parent / "data"
        data_dir.mkdir(exist_ok=True)
        url = f"sqlite:///{data_dir / 'site.db'}"
    # Neon/Render/Heroku give "postgres://" or "postgresql://"; use the psycopg 3 driver.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix):]
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    metadata.create_all(engine)
    return engine


class Store:
    def __init__(self, engine: Engine):
        self.engine = engine

    # --- writes -----------------------------------------------------------
    def add_event(self, visitor_id: str, event: str, detail: str = "", source: str = "", ip_hash: str = "") -> None:
        with self.engine.begin() as conn:
            conn.execute(events.insert().values(
                created_at=now(), visitor_id=visitor_id, event=event,
                detail=detail[:200], source=source[:100], ip_hash=ip_hash,
            ))

    def add_feedback(self, **values: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(feedback.insert().values(created_at=now(), **values))

    def add_signup(self, **values: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(signups.insert().values(created_at=now(), **values))

    def add_owner(self, visitor_id: str) -> None:
        """Mark a browser as the owner's, so its visits never show up in the stats."""
        if not visitor_id:
            return
        with self.engine.begin() as conn:
            known = conn.execute(select(owners.c.visitor_id).where(owners.c.visitor_id == visitor_id)).first()
            if not known:
                conn.execute(owners.insert().values(visitor_id=visitor_id, created_at=now()))

    def is_owner(self, visitor_id: str) -> bool:
        with self.engine.connect() as conn:
            return conn.execute(select(owners.c.visitor_id).where(owners.c.visitor_id == visitor_id)).first() is not None

    # --- rate limiting -----------------------------------------------------
    def invoices_today(self) -> int:
        """Invoices processed by everyone today (global cap that protects the API budget)."""
        start = now().replace(hour=0, minute=0, second=0, microsecond=0)
        query = select(func.count()).select_from(events).where(
            events.c.event == "invoice_processed", events.c.created_at >= start
        )
        with self.engine.connect() as conn:
            return conn.execute(query).scalar_one()

    def invoices_used(self, ip_hash: str, visitor_id: str) -> int:
        """Free invoices one visitor has used, ever. Counted by browser id AND by IP,
        so clearing the browser alone does not reset the free trial."""
        base = select(func.count()).select_from(events).where(events.c.event == "invoice_processed")
        with self.engine.connect() as conn:
            by_ip = conn.execute(base.where(events.c.ip_hash == ip_hash)).scalar_one() if ip_hash else 0
            by_visitor = conn.execute(base.where(events.c.visitor_id == visitor_id)).scalar_one() if visitor_id else 0
        return max(by_ip, by_visitor)

    # --- admin stats (owner's own browsers excluded) ----------------------------
    def stats(self, start: datetime | None = None, end: datetime | None = None,
              tz_minutes: int = 0) -> dict[str, Any]:
        """Stats for the period [start, end) - all time when both are None.

        ``tz_minutes`` is the admin's UTC offset (e.g. 180 for UTC+3): it decides where
        "today" and the days of the 14-day chart begin and end, and how times are shown.
        """
        tz = timezone(timedelta(minutes=tz_minutes))

        def period(column):
            # SQLite stores times as naive UTC text, so always compare in UTC.
            conds = []
            if start is not None:
                conds.append(column >= start.astimezone(timezone.utc))
            if end is not None:
                conds.append(column < end.astimezone(timezone.utc))
            return conds

        ev, fb, su = period(events.c.created_at), period(feedback.c.created_at), period(signups.c.created_at)

        def visitors_with(conn, *names: str) -> int:
            query = select(func.count(func.distinct(events.c.visitor_id))).where(
                events.c.event.in_(names), NOT_OWNER_EV, *ev)
            return conn.execute(query).scalar_one()

        local_now = now().astimezone(tz)
        today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = today - timedelta(days=13)
        with self.engine.connect() as conn:
            funnel = [
                ("Visited the site", visitors_with(conn, "visit")),
                ("Tried the demo", visitors_with(conn, "demo_sample", "demo_upload")),
                ("Uploaded own invoices", visitors_with(conn, "demo_upload")),
                ("Used all free invoices", visitors_with(conn, "free_limit_reached")),
                ("Downloaded the CSV", visitors_with(conn, "csv_download")),
                ("Saw pricing", visitors_with(conn, "pricing_view")),
                ("Clicked a plan", visitors_with(conn, "plan_click")),
                ("Left feedback", conn.execute(
                    select(func.count(func.distinct(feedback.c.visitor_id))).where(NOT_OWNER_FB, *fb)
                ).scalar_one()),
                ("Joined the waitlist", conn.execute(
                    select(func.count(func.distinct(func.lower(signups.c.email)))).where(NOT_OWNER_SU, *su)
                ).scalar_one()),
            ]
            processed = select(func.count()).select_from(events).where(
                events.c.event == "invoice_processed", NOT_OWNER_EV)
            invoices_period = conn.execute(processed.where(*ev)).scalar_one()
            invoices_today = conn.execute(
                processed.where(events.c.created_at >= today.astimezone(timezone.utc))).scalar_one()

            plan_clicks = dict(conn.execute(
                select(events.c.detail, func.count(func.distinct(events.c.visitor_id)))
                .where(events.c.event == "plan_click", NOT_OWNER_EV, *ev).group_by(events.c.detail)
            ).all())
            plan_signups = dict(conn.execute(
                select(signups.c.plan, func.count()).where(NOT_OWNER_SU, *su).group_by(signups.c.plan)
            ).all())
            ratings = dict(conn.execute(
                select(feedback.c.rating, func.count()).where(NOT_OWNER_FB, *fb).group_by(feedback.c.rating)
            ).all())

            sources_visits = dict(conn.execute(
                select(events.c.source, func.count(func.distinct(events.c.visitor_id)))
                .where(events.c.event == "visit", NOT_OWNER_EV, *ev).group_by(events.c.source)
            ).all())
            sources_signups = dict(conn.execute(
                select(signups.c.source, func.count()).where(NOT_OWNER_SU, *su).group_by(signups.c.source)
            ).all())

            # The 14-day chart always shows the last two weeks, whatever period is picked.
            recent_visits = conn.execute(
                select(events.c.created_at, events.c.visitor_id).where(
                    events.c.event == "visit", events.c.created_at >= since.astimezone(timezone.utc),
                    NOT_OWNER_EV)
            ).all()

            signup_rows = [dict(r._mapping) for r in conn.execute(
                select(signups).where(NOT_OWNER_SU, *su).order_by(signups.c.created_at.desc())
            )]
            feedback_rows = [dict(r._mapping) for r in conn.execute(
                select(feedback).where(NOT_OWNER_FB, *fb).order_by(feedback.c.created_at.desc())
            )]

        def local(moment: datetime) -> datetime:
            if moment.tzinfo is None:  # SQLite gives back naive UTC
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.astimezone(tz)

        daily: dict[str, set[str]] = {
            (since + timedelta(days=i)).strftime("%Y-%m-%d"): set() for i in range(14)
        }
        for created_at, visitor in recent_visits:
            day = local(created_at).strftime("%Y-%m-%d")
            if day in daily:
                daily[day].add(visitor)

        sources = sorted(
            ({"source": s or "(direct)", "visitors": v, "signups": sources_signups.get(s, 0)}
             for s, v in sources_visits.items()),
            key=lambda row: -row["visitors"],
        )
        for row in signup_rows + feedback_rows:
            row["created_at"] = local(row["created_at"]).strftime("%Y-%m-%d %H:%M")
            row.pop("visitor_id", None)
        return {
            "funnel": [{"step": step, "visitors": count} for step, count in funnel],
            "invoices_processed": {"total": invoices_period, "today": invoices_today},
            "plans": [
                {"plan": p, "clicks": plan_clicks.get(p, 0), "signups": plan_signups.get(p, 0)}
                for p in sorted(set(plan_clicks) | set(plan_signups))
            ],
            "ratings": ratings,
            "sources": sources,
            "daily_visitors": [{"day": d, "visitors": len(v)} for d, v in daily.items()],
            "signups": signup_rows,
            "feedback": feedback_rows,
        }
