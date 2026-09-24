"""New-style SQLAlchemy querying for :class:`ApiBuilder`.

Primary path is SQLAlchemy 2.0 style (``select(model)`` executed through
the builder's session). A ``QueryAdapter`` exposes the small subset of the
legacy Flask-SQLAlchemy ``Query`` API the builder (and existing
``query_override`` callables) rely on — ``filter``, ``order_by``, ``all``,
``first``, ``count``, ``paginate`` — so old overrides keep working while
new code never touches ``model.query``.
"""
import math

from sqlalchemy import func, select


class QueryAdapter:
    """Wrap a ``select()`` statement with a legacy-``Query``-like API."""

    def __init__(self, model, session, stmt=None):
        self.model = model
        self.session = session
        self._stmt = stmt if stmt is not None else select(model)

    # -- chaining ------------------------------------------------------
    def filter(self, *criteria):
        if not criteria:
            return self
        return QueryAdapter(self.model, self.session,
                            self._stmt.where(*criteria))

    def order_by(self, *criteria):
        if not criteria:
            return self
        return QueryAdapter(self.model, self.session,
                            self._stmt.order_by(*criteria))

    # -- execution -----------------------------------------------------
    def all(self):
        return list(self.session.execute(self._stmt).scalars().all())

    def first(self):
        return self.session.execute(self._stmt).scalars().first()

    def count(self):
        stmt = self._stmt
        count_stmt = select(func.count()).select_from(stmt.subquery())
        result = self.session.execute(count_stmt).scalar()
        return int(result or 0)

    def paginate(self, page=1, per_page=20, error_out=False, **kwargs):
        page = max(1, int(page or 1))
        per_page = max(1, int(per_page or 20))
        total = self.count()
        pages = max(0, math.ceil(total / per_page)) if total else 0
        items = list(self.session.execute(
            self._stmt.offset((page - 1) * per_page).limit(per_page)
        ).scalars().all())

        class _Pagination:
            pass

        pagination = _Pagination()
        pagination.items = items
        pagination.page = page
        pagination.per_page = per_page
        pagination.pages = pages
        pagination.total = total
        return pagination


def is_legacy_query(obj):
    """True for legacy Flask-SQLAlchemy ``Query`` (or adapter) objects."""
    return (
        hasattr(obj, 'filter')
        and hasattr(obj, 'all')
        and hasattr(obj, 'first')
        and hasattr(obj, 'paginate')
    )


def ensure_adapter(model, session, query):
    """Coerce ``query`` to a :class:`QueryAdapter` when it is a ``Select``."""
    from sqlalchemy.sql.selectable import Select
    if isinstance(query, QueryAdapter):
        return query
    if is_legacy_query(query):
        return query
    if isinstance(query, Select):
        return QueryAdapter(model, session, stmt=query)
    # Fallback: objects with ``where`` but no legacy API are Select-like.
    if hasattr(query, 'where') and not hasattr(query, 'filter'):
        return QueryAdapter(model, session, stmt=query)
    return query


def base_query(model, session):
    """Return a new-style :class:`QueryAdapter` for ``model``."""
    return QueryAdapter(model, session)


def legacy_query_or_adapter(model, session):
    """Return legacy ``model.query`` when explicitly needed, else adapter.

    Prefer :func:`base_query`. This helper exists for the few places that
    historically used ``model.query`` directly (e.g. ``view_args_ref``
    lookups) — new-style first, legacy fallback only if the session path
    fails and ``model.query`` is available.
    """
    return base_query(model, session)


def first_by_column(session, model, column, value):
    """New-style ``SELECT ... WHERE column == value LIMIT 1`` helper."""
    return session.execute(
        select(model).where(column == value)
    ).scalars().first()
