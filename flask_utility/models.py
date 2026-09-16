"""Framework-agnostic model mixins.

These use plain SQLAlchemy column types (no Flask-SQLAlchemy import),
so they work with any declarative base::

    from flask_utility.models import SlugMixin, TimestampMixin

    class Category(db.Model, SlugMixin, TimestampMixin):
        __tablename__ = 'categories'
        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String(255), nullable=False)

        slug_source = 'name'
        _events = ['insert', 'update']

Slug generation itself lives in :mod:`flask_utility.events` — call
:func:`flask_utility.init_model_events` once at startup.
"""
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, object_session


class BaseModel:
    """Session-aware persistence helpers (no commit policy imposed)."""

    __abstract__ = True

    def _session(self, session=None):
        if session is not None:
            return session
        session = object_session(self)
        if session is None:
            raise RuntimeError(
                'No session available: add the instance to a session '
                'first or pass session=... explicitly.'
            )
        return session

    def save(self, commit=True, session=None):
        """Add + optionally commit; rolls back and re-raises on error."""
        session = self._session(session)
        try:
            session.add(self)
            if commit:
                session.commit()
        except Exception:
            session.rollback()
            raise
        return self

    def delete(self, commit=True, session=None):
        """Delete + optionally commit; rolls back and re-raises on error."""
        session = self._session(session)
        try:
            session.delete(self)
            if commit:
                session.commit()
        except Exception:
            session.rollback()
            raise

    def update(self, commit=True, session=None, **kwargs):
        """Set attributes + optionally commit (no rollback needed)."""
        for attr, value in kwargs.items():
            setattr(self, attr, value)
        if commit:
            self._session(session).commit()
        return self


class TimestampMixin:
    """``created_at`` / ``updated_at`` columns (UTC, naive)."""

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class SlugMixin:
    """Unique-slug support driven by ``slug_source`` + ``_events``.

    Subclasses declare which field feeds the slug and on which flush
    operations it (re)generates::

        class Category(BaseModel, SlugMixin):
            slug_source = 'name'
            _events = ['insert', 'update']

    See :mod:`flask_utility.events` for the generation pipeline.
    """

    slug_source: str = 'name'
    _events = ['insert', 'update']
    slug: Mapped[str] = mapped_column(String(255), unique=True)
