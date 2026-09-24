"""Flask extension entry point.

:class:`FlaskUtility` follows the standard Flask extension pattern:
create once, initialise with the app plus the ``db`` and ``ma``
instances the bundled builders need::

    from flask_utility import FlaskUtility

    utility = FlaskUtility()
    utility.init_app(app, db=db, ma=ma)

Builders accept explicit ``db_session`` / ``ma`` arguments that take
precedence over the extension (handy for tests or multiple databases).
When neither is given, they resolve through the extension bound to the
current Flask app.

Global error shapes can be configured once on the extension and
overridden per :class:`ApiBuilder`::

    utility = FlaskUtility(errors={
        404: lambda error, ctx: {'success': False, 'message': 'Not found'},
        422: lambda error, ctx: {'success': False, 'errors': getattr(error, 'messages', str(error))},
    })

Per-builder ``errors`` are merged over these globals (builder wins per
status code / alias). See :class:`flask_utility.api_builder.ApiBuilder`
for the full ``errors`` / ``responses`` / ``overrides`` contract.
"""
from flask import current_app, has_app_context


#: Last constructed instance — resolution fallback outside app context
#: (e.g. schemas built at import time, before the app exists).
_default_instance = None


class FlaskUtility:
    """Hold shared ``db`` / ``ma`` references for bundled utilities.

    ``db``/``ma`` may be passed at construction (no app needed), which
    covers module-level schema building at import time::

        utility = FlaskUtility(db=db, ma=ma)

    then later bound to each app::

        utility.init_app(app)
    """

    def __init__(self, app=None, db=None, ma=None, errors=None, responses=None):
        global _default_instance
        self._db = db
        self._ma = ma
        self._errors = dict(errors or {})
        self._responses = dict(responses or {})
        _default_instance = self
        if app is not None:
            self.init_app(app)

    def init_app(self, app, db=None, ma=None, errors=None, responses=None):
        """Bind to ``app``, optionally (re)setting ``db`` and ``ma``.

        Args:
            app: Flask application instance.
            db: Flask-SQLAlchemy instance (provides ``db.session``).
            ma: Flask-Marshmallow instance (provides
                ``ma.SQLAlchemyAutoSchema``).
            errors: Optional mapping of HTTP status code (``400``,
                ``404``, ``422``, ``500``, ...) or alias
                (``'validation'``, ``'not_found'``, ``'generic'``,
                ``'http'``) to ``handler(error, ctx)``. Merged under
                per-builder ``errors`` (builder wins per key).
            responses: Optional mapping of semantic action
                (``'list'``, ``'paginated'``, ``'collection'``,
                ``'create'``, ``'retrieve'``, ``'update'``, ``'patch'``,
                ``'delete'``) to ``handler(data, ctx)``. Merged under
                per-builder ``responses`` (builder wins per key).
        """
        global _default_instance
        if db is not None:
            self._db = db
        if ma is not None:
            self._ma = ma
        if errors is not None:
            self._errors = dict(errors)
        if responses is not None:
            self._responses = dict(responses)
        app.extensions = getattr(app, 'extensions', {})
        app.extensions['flask_utility'] = self
        _default_instance = self
        try:
            from .events import init_model_events
            init_model_events()
        except Exception:
            pass

    # -- accessors --------------------------------------------------------

    @property
    def db(self):
        """The bound Flask-SQLAlchemy instance (or ``None``)."""
        return self._db

    @property
    def ma(self):
        """The bound Flask-Marshmallow instance (or ``None``)."""
        return self._ma

    @property
    def session(self):
        """Shortcut for ``db.session`` (or ``None`` when unbound)."""
        db = self._db
        return getattr(db, 'session', None) if db is not None else None

    @property
    def errors(self):
        """Global error-handler mapping (may be empty)."""
        return dict(getattr(self, '_errors', {}) or {})

    @property
    def responses(self):
        """Global response-handler mapping (may be empty)."""
        return dict(getattr(self, '_responses', {}) or {})


def get_extension():
    """Return the :class:`FlaskUtility` for the current app.

    Falls back to the last constructed instance when outside an app
    context (import-time schema building).
    """
    if has_app_context():
        extensions = getattr(current_app, 'extensions', {}) or {}
        bound = extensions.get('flask_utility')
        if bound is not None:
            return bound
    return _default_instance


def resolve_session(db_session=None):
    """Return an explicit session or the extension's session."""
    if db_session is not None:
        return db_session
    extension = get_extension()
    session = getattr(extension, 'session', None)
    if session is None:
        raise RuntimeError(
            'flask-utility: no SQLAlchemy session available. Pass '
            'db_session=... explicitly or bind the extension with '
            'FlaskUtility().init_app(app, db=db).'
        )
    return session


def resolve_ma(ma=None):
    """Return an explicit ``ma`` or the extension's instance."""
    if ma is not None:
        return ma
    extension = get_extension()
    instance = getattr(extension, 'ma', None)
    if instance is None:
        raise RuntimeError(
            'flask-utility: no Marshmallow instance available. Pass '
            'ma=... explicitly or bind the extension with '
            'FlaskUtility().init_app(app, ma=ma).'
        )
    return instance


def resolve_errors():
    """Return global error-handler mapping (empty dict when unbound)."""
    extension = get_extension()
    if extension is None:
        return {}
    return dict(getattr(extension, 'errors', {}) or {})


def resolve_responses():
    """Return global response-handler mapping (empty dict when unbound)."""
    extension = get_extension()
    if extension is None:
        return {}
    return dict(getattr(extension, 'responses', {}) or {})
