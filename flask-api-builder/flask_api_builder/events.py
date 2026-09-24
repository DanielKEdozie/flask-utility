"""Model event system.

Models opt into automatic unique-slug generation by declaring a
``slug_source`` field name and an ``_events`` list::

    class Category(db.Model, SlugMixin):
        slug_source = 'name'
        _events = ['insert', 'update']

``init_model_events()`` wires a single ``before_flush`` hook (safe to
call multiple times) that:

- on ``insert`` — generates the slug when ``'insert'`` is in ``_events``;
- on ``update`` — regenerates only when the source field changed and
  ``'update'`` is in ``_events`` (stable URLs when untouched).

Slugs are unique per model table (``name``, ``name-2``, ``name-3``, …).

Custom logic can subscribe to the same pipeline::

    from flask_utility import on_model_event

    @on_model_event('before_insert', MyModel)
    def stamp_code(session, instance):
        instance.code = instance.code or uuid4().hex[:8]

Supported event names: ``before_insert``, ``before_update``.
"""
from collections import defaultdict

from slugify import slugify
from sqlalchemy import event
from sqlalchemy.orm import Session, object_session


# Maps event name -> list of (model_or_None, handler). A ``None`` model
# means "all models".
_custom_handlers = defaultdict(list)

#: Models already processed in the current flush (re-entrancy guard).
_registered = False


def on_model_event(event_name, model=None):
    """Decorate a ``handler(session, instance)`` for a model event.

    Args:
        event_name: ``'before_insert'`` or ``'before_update'``.
        model: Model class to scope the handler to, or ``None`` for all.
    """
    if event_name not in ('before_insert', 'before_update'):
        raise ValueError(
            "event_name must be 'before_insert' or 'before_update'"
        )

    def decorator(handler):
        _custom_handlers[event_name].append((model, handler))
        return handler

    return decorator


def _slug_source_value(instance):
    source_attr = getattr(instance.__class__, 'slug_source', 'name')
    return getattr(instance, source_attr, None)


def _unique_slug(session, model_cls, base, exclude_id=None):
    from sqlalchemy import select
    candidate = base
    n = 2
    while True:
        # New-style select; no legacy ``session.query``.
        stmt = select(model_cls).where(model_cls.slug == candidate)
        pk = getattr(model_cls, 'id', None)
        if exclude_id is not None and pk is not None:
            stmt = stmt.where(pk != exclude_id)
        if session.execute(stmt).scalars().first() is None:
            return candidate
        candidate = '{}-{}'.format(base, n)
        n += 1


def _run_custom_handlers(event_name, session, instance):
    for model, handler in _custom_handlers.get(event_name, ()):
        if model is None or isinstance(instance, model):
            handler(session, instance)


def _source_changed(instance, source_attr):
    """True when the slug source field changed in this flush."""
    try:
        from sqlalchemy.orm.attributes import get_history
        history = get_history(instance, source_attr)
        return bool(history.added or history.deleted)
    except Exception:
        return True


def _handle_slug(session, instance, is_new):
    events = getattr(instance.__class__, '_events', ['insert', 'update'])
    source_attr = getattr(instance.__class__, 'slug_source', 'name')
    source = getattr(instance, source_attr, None)
    if not source:
        return

    slug = slugify(str(source))
    if not slug:
        return

    if is_new:
        if 'insert' not in events:
            return
        if getattr(instance, 'slug', None) == slug:
            return
    else:
        if 'update' not in events:
            return
        if getattr(instance, 'slug', None) == slug:
            return
        if not _source_changed(instance, source_attr):
            return

    instance.slug = _unique_slug(
        session,
        instance.__class__,
        slug,
        getattr(instance, 'id', None),
    )


def _before_flush(session, flush_context, instances):
    for obj in list(session.new):
        if hasattr(obj.__class__, 'slug_source') or hasattr(obj, 'slug'):
            _handle_slug(session, obj, is_new=True)
        _run_custom_handlers('before_insert', session, obj)
    for obj in list(session.dirty):
        if not session.is_modified(obj, include_collections=False):
            continue
        if hasattr(obj.__class__, 'slug_source') or hasattr(obj, 'slug'):
            _handle_slug(session, obj, is_new=False)
        _run_custom_handlers('before_update', session, obj)


def init_model_events():
    """Wire the ``before_flush`` model-event hook (idempotent)."""
    global _registered
    if _registered:
        return
    event.listen(Session, 'before_flush', _before_flush)
    _registered = True
