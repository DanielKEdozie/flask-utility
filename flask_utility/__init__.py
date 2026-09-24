"""flask-utility — reusable Flask + SQLAlchemy utilities.

Exposes a Flask extension (:class:`FlaskUtility`) that holds the
``db`` (Flask-SQLAlchemy) and ``ma`` (Flask-Marshmallow) instances, so
bundled builders and model events resolve them without importing the
host application::

    from flask_utility import FlaskUtility

    utility = FlaskUtility()
    utility.init_app(app, db=db, ma=ma)

Submodules:

- :mod:`flask_utility.api_builder` — convention-based CRUD endpoints.
- :mod:`flask_utility.api_views` — MethodView adapters (Collection/Item/Singleton).
- :mod:`flask_utility.api_helpers` — pure action/response/error helpers.
- :mod:`flask_utility.api_decorators` — view decorators (errors + custom).
- :mod:`flask_utility.querying` — new-style select() + QueryAdapter.
- :mod:`flask_utility.schema_builder` — model-driven Marshmallow schemas.
- :mod:`flask_utility.events` — model event system (auto-slug, hooks).
- :mod:`flask_utility.models` — framework-agnostic model mixins.
"""
from .extension import FlaskUtility
from .api_builder import ApiBuilder
from .api_views import CollectionView, ItemView, SingletonView
from .schema_builder import SchemaBuilder
from .events import init_model_events, on_model_event
from .models import BaseModel, SlugMixin, TimestampMixin

__all__ = [
    'FlaskUtility',
    'ApiBuilder',
    'CollectionView',
    'ItemView',
    'SingletonView',
    'SchemaBuilder',
    'init_model_events',
    'on_model_event',
    'BaseModel',
    'SlugMixin',
    'TimestampMixin',
]

__version__ = '2.0.0'
