from functools import wraps
from inspect import signature
import re

from flask import abort, jsonify, request
from marshmallow import ValidationError
from werkzeug.exceptions import HTTPException
from sqlalchemy import or_

from .extension import resolve_session


class ApiBuilder:
    """Register conventional CRUD endpoints for a SQLAlchemy model.

    ``model`` must expose a SQLAlchemy query through ``model.query`` and
    ``schema`` must be a Marshmallow schema instance or schema class. The
    builder registers a collection route and an item route immediately:

    * ``GET /parts`` lists resources.
    * ``POST /parts`` creates a resource.
    * ``GET /parts/<id>`` returns one resource.
    * ``PUT /parts/<id>`` updates a resource.
    * ``PATCH /parts/<id>`` partially updates a resource.
    * ``DELETE /parts/<id>`` deletes a resource.

    Example::

        ApiBuilder(
            api_bp,
            Part,
            PartSchema,
            endpoint='parts',
            url_prefix='/parts',
            filter_fields=('status',),
            search_fields=('name', 'sku'),
            sort_field='name',
            hooks={
                'before_create': validate_part,
                'after_delete': remove_part_images,
            },
        )

    ``methods`` controls which HTTP methods are registered. ``pk_name``
    changes the item identifier field. ``view_args`` adds URL parameters and
    maps each parameter to a model field, for example
    ``{'shop_id': 'shop_id'}`` creates ``/parts/<shop_id>`` and scopes the
    query to that field.

    ``singleton`` registers a single-resource endpoint instead of the
    collection/item pair. Use it when the URL already identifies exactly one
    row, e.g. a per-parent singleton such as
    ``/orders/<order_id>/dispatch`` where ``order_id`` is both the route
    variable and the model's primary key::

        ApiBuilder(
            api_bp,
            OrderDispatch,
            OrderDispatchSchema,
            endpoint='order_dispatch',
            url_prefix='/orders/<order_id>/dispatch',
            singleton=True,
            view_args={'order_id': 'order_id'},
            view_args_ref={'order_id': (Order, 'id')},
        )

    Singleton ``GET`` returns the row or ``404``; ``POST``/``PUT``/``PATCH``
    create it when missing (``201``) or update it when present (``200``);
    ``DELETE`` removes it. Route ``view_args`` are copied into the request
    payload on write when absent, so explicit inject hooks are unnecessary.

    Collection GET requests support exact filters from ``filter_fields``, a
    ``search`` query across ``search_fields``, and ``sort``. Pagination is
    enabled by default and returns ``items``, ``page``, ``per_page``,
    ``pages``, and ``total``. Set ``paginate=False`` to return a plain list.
    ``per_page`` and ``max_per_page`` control the default and maximum page
    sizes.

    ``query_override`` maps ``collection``, ``item``, an HTTP method, or the
    endpoint name to a callable. The callable can receive ``query``,
    ``query, route_values``, ``query, route_values, request``, or
    ``query, route_values, request, api_builder`` and must return a query.
    The request is Flask's active request proxy, so query parameters are
    available through ``request.args`` and path values through
    ``route_values``. ``api_builder`` is the configured builder instance and
    exposes the model, filters, schema, and subclass-specific attributes.
    ``decorators`` maps ``collection``, ``item``, the endpoint name, or an
    HTTP method to one decorator or a sequence of decorators.

    Subclasses may name this mapping ``query_override`` or
    ``query_overrides``.

    ``hooks`` maps ``before_create``, ``after_create``, ``before_update``,
    ``after_update``, ``before_delete``, and ``after_delete`` to one callable
    or a sequence of callables. New hooks can receive
    ``item, data, route_values, request, api_builder``. Shorter existing
    signatures, including ``hook(item)``, remain supported. ``data`` is the
    parsed request payload and ``route_values`` contains path parameters.
    Before hooks run before the database commit; after hooks run after it.
    Exceptions from loading, hooks, or persistence return a ``400`` response
    and trigger a session rollback.

    ``db_session`` optionally pins the SQLAlchemy session used for
    persistence. When omitted, it resolves through the ``FlaskUtility``
    extension bound to the current app (``init_app(app, db=db)``).

    Configuration can also be defined on a subclass. Constructor arguments
    take precedence over class attributes, so this is equivalent to passing
    the hooks to the base class constructor::

        class PartApi(ApiBuilder):
            hooks = {
                'before_create': validate_part,
                'after_update': refresh_part_cache,
            }

        PartApi(api_bp, Part, PartSchema, endpoint='parts')
    """

    def __init__(
        self,
        app_or_bp,
        model,
        schema,
        endpoint=None,
        url_prefix=None,
        pk_name='id',
        methods=None,
        singleton=None,
        filter_fields=None,
        search_fields=None,
        sort_field=None,
        sort_order=None,
        paginate=True,
        per_page=20,
        max_per_page=100,
        view_args=None,
        view_args_ref=None,
        query_override=None,
        decorators=None,
        hooks=None,
        db_session=None,
    ):
        self.app_or_bp = app_or_bp
        self.model = model
        self.schema = schema() if isinstance(schema, type) else schema
        # Explicit session wins; otherwise resolve through the FlaskUtility
        # extension bound to the current app (init_app(app, db=db)).
        self._db_session = db_session
        self.endpoint = endpoint or model.__name__.lower()
        self.url_prefix = url_prefix or '/{}'.format(self.endpoint)
        self.pk_name = pk_name
        self.methods = {method.upper() for method in (methods or ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])}
        self.singleton = (
            singleton if singleton is not None
            else bool(getattr(type(self), 'singleton', False))
        )
        self.filter_fields = tuple(filter_fields or ())
        self.search_fields = tuple(search_fields or ())
        self.sort_field = sort_field
        self.sort_order = (sort_order or 'asc').lower()
        self.paginate = paginate
        self.per_page = max(1, per_page)
        self.max_per_page = max(1, max_per_page)
        self.view_args = dict(view_args or {})
        self.view_args_ref = dict(view_args_ref or {})
        configured_query_override = (
            query_override if query_override is not None
            else getattr(type(self), 'query_override', None)
        )
        if configured_query_override is None:
            configured_query_override = getattr(type(self), 'query_overrides', None)
        configured_decorators = (
            decorators if decorators is not None
            else getattr(type(self), 'decorators', None)
        )
        configured_hooks = (
            hooks if hooks is not None
            else getattr(type(self), 'hooks', None)
        )
        self.query_override = dict(configured_query_override or {})
        raw_decorators = dict(configured_decorators or {})
        # Accept a bare decorator or a sequence per key.
        self.decorators = {
            key: (value,) if callable(value) else tuple(value or ())
            for key, value in raw_decorators.items()
        }
        self.hooks = dict(configured_hooks or {})

        if self.sort_order not in {'asc', 'desc'}:
            raise ValueError("sort_order must be 'asc' or 'desc'")

        self._register_routes()

    @property
    def session(self):
        """Active SQLAlchemy session (explicit or via extension)."""
        return resolve_session(self._db_session)

    def _register_routes(self):
        collection_methods = self.methods.intersection({'GET', 'POST'})
        item_methods = self.methods.intersection({'GET', 'PUT', 'PATCH', 'DELETE'})
        route_variables = set(re.findall(
            r'<(?:[^:<>]+:)?([^<>]+)>', self.url_prefix
        ))
        view_suffix = ''.join(
            '/<{}>'.format(name)
            for name in self.view_args
            if name not in route_variables
        )

        if self.singleton:
            singleton_methods = self.methods.intersection(
                {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}
            )
            if singleton_methods:
                self.app_or_bp.add_url_rule(
                    self.url_prefix + view_suffix,
                    endpoint='{}_singleton'.format(self.endpoint),
                    view_func=self._decorate(
                        self._json_errors(self._singleton),
                        'singleton',
                        'singleton',
                    ),
                    methods=sorted(singleton_methods),
                )
            return

        if self.pk_name in route_variables:
            raise ValueError(
                "pk_name '{}' already appears in url_prefix '{}'; ".format(
                    self.pk_name, self.url_prefix
                )
                + "use singleton=True for single-resource URLs."
            )

        if collection_methods:
            self.app_or_bp.add_url_rule(
                self.url_prefix + view_suffix,
                endpoint='{}_collection'.format(self.endpoint),
                view_func=self._decorate(
                    self._json_errors(self._collection),
                    'collection',
                    'collection',
                ),
                methods=sorted(collection_methods),
            )
        if item_methods:
            self.app_or_bp.add_url_rule(
                self.url_prefix + view_suffix + '/<{}>'.format(self.pk_name),
                endpoint='{}_detail'.format(self.endpoint),
                view_func=self._decorate(
                    self._json_errors(self._item),
                    'detail',
                    'item',
                ),
                methods=sorted(item_methods),
            )

    @staticmethod
    def _json_errors(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            try:
                return view_func(*args, **kwargs)
            except HTTPException as error:
                return jsonify({
                    'message': error.description,
                    'status': error.code,
                }), error.code

        return wrapped

    @staticmethod
    def _validation_response(error):
        return jsonify({
            'message': 'Validation Error',
            'errors': error.messages,
        }), 400

    def _decorate(self, view_func, method, resource_name):
        decorators = []
        decorators.extend(self.decorators.get(resource_name, ()))
        decorators.extend(self.decorators.get(self.endpoint, ()))
        method_decorators = ()
        if method.upper() in {'GET', 'POST', 'PUT', 'DELETE', 'PATCH'}:
            decorators.extend(self.decorators.get(method.upper(), ()))
            decorators.extend(self.decorators.get(method.lower(), ()))
        else:
            base_view = view_func
            method_decorators = tuple(self.decorators.get('GET', ()))
            method_decorators += tuple(self.decorators.get('get', ()))
            method_decorators += tuple(self.decorators.get('POST', ()))
            method_decorators += tuple(self.decorators.get('post', ()))
            method_decorators += tuple(self.decorators.get('PUT', ()))
            method_decorators += tuple(self.decorators.get('put', ()))
            method_decorators += tuple(self.decorators.get('DELETE', ()))
            method_decorators += tuple(self.decorators.get('delete', ()))
            method_decorators += tuple(self.decorators.get('PATCH', ()))
            method_decorators += tuple(self.decorators.get('patch', ()))

            def dispatch(*args, **kwargs):
                selected = []
                for decorator_method in (request.method, request.method.lower()):
                    selected.extend(self.decorators.get(decorator_method, ()))
                decorated_view = base_view
                for decorator in reversed(selected):
                    decorated_view = decorator(decorated_view)
                return decorated_view(*args, **kwargs)

            view_func = wraps(base_view)(dispatch)

        if not decorators and not method_decorators:
            return view_func

        decorated = view_func
        for decorator in reversed(decorators):
            decorated = decorator(decorated)
        return wraps(view_func)(decorated)

    def _get_column(self, name):
        column = getattr(self.model, name, None)
        if column is None:
            raise ValueError("Unknown {} field: {}".format(self.model.__name__, name))
        return column

    def _validate_view_args(self, route_values):
        """Ensure configured parent route resources exist before querying."""
        for argument, reference in self.view_args_ref.items():
            if argument not in route_values:
                continue

            if isinstance(reference, dict):
                reference_model = reference.get('model')
                reference_column = reference.get('column', 'id')
            elif isinstance(reference, (tuple, list)) and len(reference) == 2:
                reference_model, reference_column = reference
            else:
                raise ValueError(
                    "view_args_ref['{}'] must be (model, column) or a mapping".format(
                        argument
                    )
                )

            if reference_model is None:
                raise ValueError(
                    "view_args_ref['{}'] is missing model".format(argument)
                )

            reference_value = route_values[argument]
            column_name = (
                reference_column
                if isinstance(reference_column, str)
                else getattr(reference_column, 'key', None)
            )
            if column_name is None:
                raise ValueError(
                    "view_args_ref['{}'] column must be a field name or model column".format(
                        argument
                    )
                )

            if column_name == 'id':
                found = self.session.get(reference_model, reference_value)
            else:
                column = getattr(reference_model, column_name, None)
                if column is None:
                    raise ValueError(
                        "Unknown reference field: {}.{}".format(
                            reference_model.__name__, column_name
                        )
                    )
                found = reference_model.query.filter(
                    column == reference_value
                ).first()

            if found is None:
                abort(404, description='{} not found'.format(
                    reference_model.__name__
                ))

    def _query(self, route_values, operation=None, resource_name=None):
        query = self.model.query
        override = (
            self.query_override.get(resource_name)
            or self.query_override.get(operation)
            or self.query_override.get((operation or '').lower())
            or self.query_override.get(self.endpoint)
        )
        if override:
            try:
                accepts_context = signature(override).bind(
                    query, route_values, request, self
                )
            except (TypeError, ValueError):
                accepts_context = None
            if accepts_context is not None:
                query = override(query, route_values, request, self)
            else:
                try:
                    accepts_request = signature(override).bind(
                        query, route_values, request
                    )
                except (TypeError, ValueError):
                    accepts_request = None
                if accepts_request is not None:
                    query = override(query, route_values, request)
                else:
                    try:
                        accepts_route_values = signature(override).bind(
                            query, route_values
                        )
                    except (TypeError, ValueError):
                        accepts_route_values = None
                    query = (
                        override(query, route_values)
                        if accepts_route_values is not None
                        else override(query)
                    )

        for argument, field_name in self.view_args.items():
            query = query.filter(self._get_column(field_name) == route_values[argument])

        for field_name in self.filter_fields:
            if field_name in request.args:
                query = query.filter(self._get_column(field_name) == request.args[field_name])

        search = request.args.get('search')
        if search and self.search_fields:
            query = query.filter(or_(*(
                self._get_column(field_name).ilike('%{}%'.format(search))
                for field_name in self.search_fields
            )))

        sort_field = request.args.get('sort', self.sort_field)
        sort_order = request.args.get('sort_order', self.sort_order)
        if sort_field:
            column = self._get_column(sort_field)
            query = query.order_by(column.desc() if sort_order == 'desc' else column.asc())
        return query

    def _serialize(self, value, many=False):
        return self.schema.dump(value, many=many)

    def _load(self, data, instance=None, partial=False):
        if instance is None:
            return self.schema.load(data)
        return self.schema.load(data, instance=instance, partial=partial)

    def _run_hooks(self, name, item, data=None, route_values=None):
        configured_hooks = self.hooks.get(name, ())
        if callable(configured_hooks):
            configured_hooks = (configured_hooks,)
        for hook in configured_hooks:
            arguments = (item, data, route_values or {}, request, self)
            try:
                hook_signature = signature(hook)
            except (TypeError, ValueError):
                hook_signature = None

            if hook_signature is None:
                hook(item)
                continue

            try:
                hook_signature.bind(*arguments)
            except TypeError:
                for argument_count in range(len(arguments) - 1, 0, -1):
                    try:
                        hook_signature.bind(*arguments[:argument_count])
                    except TypeError:
                        continue
                    hook(*arguments[:argument_count])
                    break
                else:
                    hook(item)
            else:
                hook(*arguments)

    def _inject_view_args(self, data, route_values):
        """Copy route ``view_args`` into the write payload when absent.

        Keeps nested writes (e.g. ``POST /products/<id>/images``) scoped
        to the URL without requiring the client to repeat the parent key.
        """
        for argument, field_name in self.view_args.items():
            if argument in route_values and field_name not in data:
                data[field_name] = route_values[argument]
        return data

    def _collection(self, **route_values):
        self._validate_view_args(route_values)
        if request.method == 'POST':
            try:
                data = self._inject_view_args(
                    request.get_json(silent=True) or {}, route_values
                )
                item = self._load(data)
                self._run_hooks('before_create', item, data, route_values)
                self.session.add(item)
                self.session.commit()
                self._run_hooks('after_create', item, data, route_values)
            except ValidationError as error:
                self.session.rollback()
                return self._validation_response(error)
            except Exception as error:
                self.session.rollback()
                return jsonify(message=str(error)), 400
            return jsonify(self._serialize(item)), 201

        query = self._query(route_values, request.method, 'collection')
        if not self.paginate:
            return jsonify(self._serialize(query.all(), many=True))

        page = max(1, request.args.get('page', default=1, type=int))
        per_page = min(
            self.max_per_page,
            max(1, request.args.get('per_page', default=self.per_page, type=int)),
        )
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        return jsonify({
            'items': self._serialize(pagination.items, many=True),
            'page': pagination.page,
            'per_page': pagination.per_page,
            'pages': pagination.pages,
            'total': pagination.total,
        })

    def _item(self, **route_values):
        self._validate_view_args(route_values)
        item = self._query(route_values, request.method, 'item').filter(
            self._get_column(self.pk_name) == route_values[self.pk_name]
        ).first()
        if item is None:
            return jsonify(message='Resource not found'), 404

        if request.method == 'GET':
            return jsonify(self._serialize(item))
        if request.method == 'DELETE':
            try:
                self._run_hooks('before_delete', item, None, route_values)
                self.session.delete(item)
                self.session.commit()
                self._run_hooks('after_delete', item, None, route_values)
            except ValidationError as error:
                self.session.rollback()
                return self._validation_response(error)
            except Exception as error:
                self.session.rollback()
                return jsonify(message=str(error)), 400
            return '', 204

        try:
            data = self._inject_view_args(
                request.get_json(silent=True) or {}, route_values
            )
            self._load(data, instance=item, partial=True)
            self._run_hooks('before_update', item, data, route_values)
            self.session.commit()
            self._run_hooks('after_update', item, data, route_values)
        except ValidationError as error:
            self.session.rollback()
            return self._validation_response(error)
        except Exception as error:
            self.session.rollback()
            return jsonify(message=str(error)), 400
        return jsonify(self._serialize(item))

    def _singleton(self, **route_values):
        """Serve a single-resource endpoint (``singleton=True``).

        ``GET`` returns the row or ``404``. ``POST``, ``PUT`` and ``PATCH``
        create the row when missing (``201``) or update it when present
        (``200``). ``DELETE`` removes it or returns ``404``.
        """
        self._validate_view_args(route_values)
        query = self._query(route_values, request.method, 'singleton')
        item = query.first()

        if request.method == 'GET':
            if item is None:
                return jsonify(message='Resource not found'), 404
            return jsonify(self._serialize(item))

        if request.method == 'DELETE':
            if item is None:
                return jsonify(message='Resource not found'), 404
            try:
                self._run_hooks('before_delete', item, None, route_values)
                self.session.delete(item)
                self.session.commit()
                self._run_hooks('after_delete', item, None, route_values)
            except ValidationError as error:
                self.session.rollback()
                return self._validation_response(error)
            except Exception as error:
                self.session.rollback()
                return jsonify(message=str(error)), 400
            return '', 204

        try:
            data = self._inject_view_args(
                request.get_json(silent=True) or {}, route_values
            )
            if item is None:
                item = self._load(data)
                self._run_hooks('before_create', item, data, route_values)
                self.session.add(item)
                self.session.commit()
                self._run_hooks('after_create', item, data, route_values)
                return jsonify(self._serialize(item)), 201
            self._load(data, instance=item, partial=True)
            self._run_hooks('before_update', item, data, route_values)
            self.session.commit()
            self._run_hooks('after_update', item, data, route_values)
        except ValidationError as error:
            self.session.rollback()
            return self._validation_response(error)
        except Exception as error:
            self.session.rollback()
            return jsonify(message=str(error)), 400
        return jsonify(self._serialize(item))