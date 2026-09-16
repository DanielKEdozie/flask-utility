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

    def __init__(self, app=None, db=None, ma=None):
        global _default_instance
        self._db = db
        self._ma = ma
        _default_instance = self
        if app is not None:
            self.init_app(app)

    def init_app(self, app, db=None, ma=None):
        """Bind to ``app``, optionally (re)setting ``db`` and ``ma``.

        Args:
            app: Flask application instance.
            db: Flask-SQLAlchemy instance (provides ``db.session``).
            ma: Flask-Marshmallow instance (provides
                ``ma.SQLAlchemyAutoSchema``).
        """
        global _default_instance
        if db is not None:
            self._db = db
        if ma is not None:
            self._ma = ma
        app.extensions = getattr(app, 'extensions', {})
        app.extensions['flask_utility'] = self
        _default_instance = self

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
