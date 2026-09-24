"""flask-api-builder — convention-based CRUD endpoints for SQLAlchemy and Marshmallow.

Exposes :class:`ApiBuilder` and the companion Flask extension :class:`FlaskApiBuilder`::

    from flask import Flask, Blueprint
    from flask_sqlalchemy import SQLAlchemy
    from flask_marshmallow import Marshmallow
    from flask_api_builder import FlaskApiBuilder, ApiBuilder

    app = Flask(__name__)
    db = SQLAlchemy(app)
    ma = Marshmallow(app)

    api_ext = FlaskApiBuilder()
    api_ext.init_app(app, db=db)

    api_bp = Blueprint('api', __name__, url_prefix='/api')
    ApiBuilder(api_bp, Product, ProductSchema, endpoint='products')
"""
from .extension import (
    FlaskApiBuilder,
    get_extension,
    resolve_errors,
    resolve_ma,
    resolve_responses,
    resolve_session,
)
from .builder import ApiBuilder
from .api_views import CollectionView, ItemView, SingletonView

__all__ = [
    'ApiBuilder',
    'CollectionView',
    'ItemView',
    'SingletonView',
    'FlaskApiBuilder',
    'get_extension',
    'resolve_session',
    'resolve_ma',
    'resolve_errors',
    'resolve_responses',
]

__version__ = '2.0.0'
