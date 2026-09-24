"""Extension entry point for flask-api-builder.

:class:`FlaskApiBuilder` follows the standard Flask extension pattern:
create once, initialise with the app plus ``db``, global ``responses``,
and global ``errors``::

    from flask_api_builder import FlaskApiBuilder

    api_ext = FlaskApiBuilder()
    api_ext.init_app(app, db=db, responses={...}, errors={...})
"""
from flask import current_app, has_app_context

_default_instance = None


class FlaskApiBuilder:
    """Hold shared ``db`` / ``errors`` / ``responses`` references for ApiBuilder."""

    def __init__(self, app=None, db=None, errors=None, responses=None, ma=None):
        global _default_instance
        self._db = db
        self._ma = ma
        self._errors = dict(errors or {})
        self._responses = dict(responses or {})
        _default_instance = self
        if app is not None:
            self.init_app(app)

    def init_app(self, app, db=None, errors=None, responses=None, ma=None):
        """Bind to ``app``, setting ``db``, ``errors``, and ``responses``."""
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
        app.extensions['flask_api_builder'] = self
        _default_instance = self

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
        """Global error-handler mapping."""
        return dict(getattr(self, '_errors', {}) or {})

    @property
    def responses(self):
        """Global response-handler mapping."""
        return dict(getattr(self, '_responses', {}) or {})


def get_extension():
    """Return the active extension for the current app.

    Checks for ``flask_api_builder`` first, and falls back to
    ``flask_utility`` if bound in the host application.
    """
    if has_app_context():
        extensions = getattr(current_app, 'extensions', {}) or {}
        if 'flask_api_builder' in extensions:
            return extensions['flask_api_builder']
        if 'flask_utility' in extensions:
            return extensions['flask_utility']
    return _default_instance


def resolve_session(db_session=None):
    """Return an explicit session or the extension's session."""
    if db_session is not None:
        return db_session
    extension = get_extension()
    session = getattr(extension, 'session', None)
    if session is None:
        raise RuntimeError(
            'flask-api-builder: no SQLAlchemy session available. Pass '
            'db_session=... explicitly or bind the extension with '
            'FlaskApiBuilder().init_app(app, db=db).'
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
            'flask-api-builder: no Marshmallow instance available. Pass '
            'ma=... explicitly or bind the extension with '
            'FlaskApiBuilder().init_app(app, ma=ma).'
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
