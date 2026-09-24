import inspect
from functools import wraps
from inspect import Parameter, signature
import re

from flask import abort, jsonify, make_response, request
from marshmallow import ValidationError
from werkzeug.exceptions import HTTPException
from sqlalchemy import or_, select

from .api_decorators import apply_decorators, make_json_errors
from .api_helpers import (
    _CANONICAL_ACTIONS,
    _ERROR_ALIAS_VARIANTS,
    _action_candidates,
    _coerce_schema,
    _normalize_action_key,
    _normalize_error_key,
    _resolve_action_value,
    apply_headers,
    build_response,
    call_error_handler,
    call_with_data_ctx,
    canonical_action,
    coerce_schema,
    default_status_for,
    is_flask_response,
    is_headers,
    normalize_action_key,
    normalize_error_key,
    normalize_handler_result,
    resolve_action_value,
)
from .extension import resolve_errors, resolve_responses, resolve_session
from .querying import QueryAdapter, base_query, ensure_adapter, first_by_column


class ApiBuilder:
    """Register conventional CRUD endpoints for a SQLAlchemy model.

    ``model`` must be a SQLAlchemy declarative model; querying is new-style
    (``select(model)`` executed through the builder's session via
    :mod:`querying`). Legacy ``model.query`` is never required — existing
    ``query_override`` callables keep working because they receive a
    ``QueryAdapter`` exposing ``filter/order_by/all/first/count/paginate``.
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
    ``query`` is a new-style ``QueryAdapter`` (``select(model)`` + session)
    exposing ``filter/order_by/all/first/count/paginate``; legacy
    ``Query`` objects and raw ``select()`` statements are also accepted as
    return values. The request is Flask's active request proxy, so query
    parameters are available through ``request.args`` and path values
    through ``route_values``. ``api_builder`` is the configured builder
    instance and exposes the model, filters, schema, and subclass-specific
    attributes.
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

    ``overrides`` (alias ``action_override`` / ``action_overrides``) maps a
    semantic action — ``list``, ``create``, ``retrieve``, ``update``,
    ``patch``, ``delete`` — to a callable that fully replaces the default
    handler for that action. ``update`` covers ``PUT`` and ``PATCH`` unless
    a dedicated ``patch`` entry exists (``PATCH`` prefers ``patch`` then
    falls back to ``update``). Legacy keys (``collection``, ``item``,
    ``singleton``, ``GET``/``POST``/..., ``POST_collection``, endpoint name)
    keep working and are resolved most-specific-first. Example::

        ApiBuilder(Part, PartSchema, ...,
            overrides={'create': audit_create, 'list': cached_list})

    Override handlers receive context by name and/or leading position.
    Available names: ``query`` (list), ``item`` (retrieve/update/delete,
    ``None`` for collection-create and missing singletons), ``data``
    (parsed JSON with ``view_args`` injected for writes), ``route_values``,
    ``request``, ``builder`` (also ``api_builder``), ``action``,
    ``schema``, ``session``. Any subset works. Handlers return plain data —
    the builder owns ``jsonify``/status/headers (Flask view conventions):
    ``data``, ``(data, status)``, ``(data, headers)``, ``(data, status,
    headers)``, or a Flask ``Response`` (passes through)::

        def audit_create(data, builder):
            item = builder.schema.load(data)
            builder.session.add(item)
            builder.session.commit()
            return builder.schema.dump(item), 201

        def cached_list(query, builder):
            return builder.schema.dump(query.all(), many=True)

        def with_headers(query, builder):
            return {'items': [...]}, 200, {'X-Total': '3'}

    Overrides still run inside ``_validate_view_args``, per-method
    ``decorators``, and the ``errors`` pipeline (``ValidationError`` and
    generic exceptions are converted via ``errors`` with a rollback).

    ``schemas`` (alias ``response_schemas``) maps an action to the
    Marshmallow schema used for that action (schema instance, schema
    class, or ``SchemaBuilder``). Falls back to the default ``schema``::

        ApiBuilder(..., schemas={
            'list': PartListSchema,      # lean list shape
            'retrieve': PartDetailSchema,
            'create': PartCreateSchema,  # write schema
        })

    ``responses`` maps an action (``'list'``, ``'paginated'``,
    ``'collection'``, ``'retrieve'``, ``'create'``, ``'update'``,
    ``'delete'``) to ``handler(data, ctx)`` shaping the success payload.
    Per-builder ``responses`` merge over ``FlaskUtility(responses=...)``
    globals (builder wins per action key). ``data`` is already dumped with
    the resolved schema (pagination dict for ``paginated`` list); ``ctx`` is
    ``{'action', 'status', 'request', 'route_values', 'builder'}``.
    Return plain ``data`` (status is preserved) or ``(data, status)`` /
    ``(data, status, headers)``; a Flask ``Response`` passes through
    untouched. Short form ``handler(data)`` is accepted::

        FlaskUtility(responses={
            'paginated': lambda data, ctx: {'data': data['items'], 'pagination': data},
            'collection': lambda data, ctx: {'data': data, 'count': len(data)},
        })

    Defaults are unchanged (plain dump; ``201`` create, ``200`` read/update,
    paginated ``list``). ``delete`` keeps ``('', 204)`` unless
    ``responses['delete']`` is explicitly defined.

    ``errors`` maps an HTTP status code (``400``, ``404``, ``422``,
    ``500``, ...) or alias (``'validation'``/``'schema'``,
    ``'not_found'``, ``'generic'``, ``'http'``) to ``handler(error, ctx)``.
    ``ctx`` is ``{'action', 'status', 'request', 'route_values', 'builder',
    'error_kind'}``. Return plain ``body`` (status code is inferred from
    ``ctx['status']`` automatically) or ``(body, status)`` / ``(body,
    status, headers)`` when overriding the status code; the builder owns
    ``jsonify``. Validation is conventional ``422`` with ``400`` fallback:
    ``422`` / ``'validation'`` / ``'schema'`` / ``'unprocessable_entity'``
    resolve to HTTP 422; a ``400``-keyed handler forces legacy HTTP 400.
    Resolution: specific code first, then alias (``422`` → validation
    aliases, ``400`` → ``'validation'``/``'generic'``, ``404`` →
    ``'not_found'``, any ``HTTPException`` → ``'http'``).
    Per-builder ``errors`` merge over ``FlaskUtility(errors=...)`` globals
    (builder wins per key)::

        FlaskUtility(errors={404: lambda e, ctx: {'success': False, 'message': 'Not found'}})
        ApiBuilder(..., errors={
            422: lambda e, ctx: {'message': 'Invalid', 'errors': e.messages},
            'schema': lambda e, ctx: {'message': 'Invalid', 'errors': e.messages},
        })

    Defaults: ``422 {'message': 'Validation Error', 'errors'}`` for
    ``ValidationError`` (``400`` only when a ``400``-keyed handler is
    configured), ``404 {'message': 'Resource not found'}`` for missing rows,
    ``{message, status}`` for ``HTTPException``, ``400 {'message': str(error)}``
    for generic failures.

    ``db_session`` optionally pins the SQLAlchemy session used for
    persistence. When omitted, it resolves through the ``FlaskUtility``
    extension bound to the current app (``init_app(app, db=db)``).

    v2 ``method_endpoints`` (alias ``endpoint_names``) customises the
    per-action Flask endpoint suffix appender. Defaults are canonical —
    ``api.products_list``, ``api.products_create``,
    ``api.products_retrieve``, ``api.products_update``,
    ``api.products_patch``, ``api.products_delete`` — resolved with the
    same candidate chain as ``overrides``. Example::

        ApiBuilder(..., method_endpoints={'retrieve': 'detail'})
        # → api.products_detail instead of api.products_retrieve

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
        schema=None,
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
        overrides=None,
        action_override=None,
        action_overrides=None,
        schemas=None,
        response_schemas=None,
        responses=None,
        response_handlers=None,
        errors=None,
        error_handlers=None,
        extra_actions=None,
        extra_methods=None,
        method_endpoints=None,
        endpoint_names=None,
    ):
        self.app_or_bp = app_or_bp
        self.model = model
        self.schema = _coerce_schema(schema)
        # Explicit session wins; otherwise resolve through the FlaskUtility
        # extension bound to the current app (init_app(app, db=db)).
        self._db_session = db_session
        self.endpoint = endpoint or model.__name__.lower()
        self.url_prefix = url_prefix if url_prefix is not None else '/{}'.format(self.endpoint)
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
        configured_overrides = overrides
        if configured_overrides is None:
            configured_overrides = action_override
        if configured_overrides is None:
            configured_overrides = action_overrides
        if configured_overrides is None:
            configured_overrides = getattr(type(self), 'overrides', None)
        if configured_overrides is None:
            configured_overrides = getattr(type(self), 'action_override', None)
        if configured_overrides is None:
            configured_overrides = getattr(type(self), 'action_overrides', None)
        configured_schemas = schemas
        if configured_schemas is None:
            configured_schemas = response_schemas
        if configured_schemas is None:
            configured_schemas = getattr(type(self), 'schemas', None)
        if configured_schemas is None:
            configured_schemas = getattr(type(self), 'response_schemas', None)
        configured_responses = responses
        if configured_responses is None:
            configured_responses = response_handlers
        if configured_responses is None:
            configured_responses = getattr(type(self), 'responses', None)
        if configured_responses is None:
            configured_responses = getattr(type(self), 'response_handlers', None)
        configured_errors = errors
        if configured_errors is None:
            configured_errors = error_handlers
        if configured_errors is None:
            configured_errors = getattr(type(self), 'errors', None)
        if configured_errors is None:
            configured_errors = getattr(type(self), 'error_handlers', None)
        configured_method_endpoints = method_endpoints
        if configured_method_endpoints is None:
            configured_method_endpoints = endpoint_names
        if configured_method_endpoints is None:
            configured_method_endpoints = getattr(type(self), 'method_endpoints', None)
        if configured_method_endpoints is None:
            configured_method_endpoints = getattr(type(self), 'endpoint_names', None)
        self.query_override = dict(configured_query_override or {})
        raw_decorators = dict(configured_decorators or {})
        # Accept a bare decorator or a sequence per key.
        self.decorators = {
            key: (value,) if callable(value) else tuple(value or ())
            for key, value in raw_decorators.items()
        }
        self.hooks = dict(configured_hooks or {})
        self.overrides = dict(configured_overrides or {})
        self.schemas = dict(configured_schemas or {})
        self.responses = dict(configured_responses or {})
        self.errors = {
            _normalize_error_key(key): value
            for key, value in dict(configured_errors or {}).items()
        }
        # v2: per-action endpoint suffix appender. Empty dict = canonical
        # action names (list/create/retrieve/update/patch/delete).
        self.method_endpoints = dict(configured_method_endpoints or {})
        configured_extra = {}
        for candidate in (
            getattr(type(self), 'extra_methods', None),
            getattr(type(self), 'extra_actions', None),
            extra_methods,
            extra_actions,
        ):
            if candidate:
                configured_extra.update(candidate)
        self.extra_actions = configured_extra

        if self.sort_order not in {'asc', 'desc'}:
            raise ValueError("sort_order must be 'asc' or 'desc'")

        self._register_routes()

    @property
    def session(self):
        """Active SQLAlchemy session (explicit or via extension)."""
        return resolve_session(self._db_session)

    def _endpoint_for(self, action, resource, method):
        """Resolve endpoint suffix via ``method_endpoints`` (v2).

        Same candidate chain as ``overrides`` (``METHOD_resource``,
        ``METHOD_action``, ``action_resource``, ``action``, ``resource``,
        ``METHOD``). Defaults to the canonical action name
        (``list/create/retrieve/update/patch/delete``); ``PATCH`` without a
        ``patch`` entry falls back to ``update``. ``detail`` is accepted as
        a legacy alias for ``retrieve``. Final Flask endpoint is
        ``'{endpoint}_{suffix}'`` with unsafe chars replaced by ``_``.
        """
        raw = self._resolve_action_value(
            self.method_endpoints, action, resource, method
        )
        suffix = str(raw or action or resource or method or '').strip().lower()
        if not suffix:
            suffix = 'action'
        if suffix == 'detail':
            # Documented legacy alias; canonical is 'retrieve'.
            suffix = 'retrieve'
        suffix = re.sub(r'[^a-z0-9_]+', '_', suffix).strip('_') or 'action'
        return '{}_{}'.format(self.endpoint, suffix)

    def _register_routes(self):
        """Register per-action ``MethodView`` endpoints (v2).

        Each action gets its own Flask endpoint via ``method_endpoints``
        (default ``{endpoint}_{action}`` → e.g. ``api.products_list``,
        ``api.products_create``). Same URL may host several endpoints with
        disjoint methods (``GET`` vs ``POST`` on the collection rule).
        Views delegate to ``do_list/do_create/do_retrieve/do_update/
        do_delete`` (+ singleton variants); legacy ``_collection/_item/
        _singleton`` dispatchers are kept and behave identically.
        """
        from .api_views import CollectionView, ItemView, SingletonView

        route_variables = set(re.findall(
            r'<(?:[^:<>]+:)?([^<>]+)>', self.url_prefix
        ))
        view_suffix = ''.join(
            '/<{}>'.format(name)
            for name in self.view_args
            if name not in route_variables
        )
        self._view_suffix = view_suffix

        def _wrap(view_cls, endpoint, resource, detail_method, methods):
            view_func = view_cls.as_view(endpoint, builder=self)
            wrapped = self._decorate(
                self._wrap_view_errors(view_func, resource),
                detail_method,
                resource,
            )
            return wrapped, sorted(methods)

        # Group by (rule, endpoint) so custom ``method_endpoints`` mapping
        # several actions to one suffix (e.g. ``{'collection': 'all'}``)
        # registers a single rule with merged methods instead of clashing.
        grouped = {}

        def _add(rule, view_cls, action, resource, method, detail_method):
            endpoint = self._endpoint_for(action, resource, method)
            key = (rule, endpoint)
            entry = grouped.get(key)
            if entry is None:
                grouped[key] = {
                    'view_cls': view_cls, 'resource': resource,
                    'detail_method': detail_method, 'methods': {method},
                }
            else:
                entry['methods'].add(method)

        def _flush():
            for (rule, endpoint), entry in grouped.items():
                wrapped, methods = _wrap(
                    entry['view_cls'], endpoint, entry['resource'],
                    entry['detail_method'], entry['methods'],
                )
                self.app_or_bp.add_url_rule(
                    rule, endpoint=endpoint, view_func=wrapped,
                    methods=methods, strict_slashes=False,
                )
            grouped.clear()

        if self.singleton:
            rule = self.url_prefix + view_suffix
            plan = [
                ('retrieve', 'singleton', 'GET'),
                ('create', 'singleton', 'POST'),
                ('update', 'singleton', 'PUT'),
                ('patch', 'singleton', 'PATCH'),
                ('delete', 'singleton', 'DELETE'),
            ]
            for action, resource, method in plan:
                if method in self.methods:
                    _add(rule, SingletonView, action, resource, method, 'singleton')
            _flush()
            # Legacy v1 endpoint alias for production backward compatibility
            legacy_singleton = '{}_singleton'.format(self.endpoint)
            active_methods = [m for _, _, m in plan if m in self.methods]
            if active_methods:
                wrapped, methods = _wrap(
                    SingletonView, legacy_singleton, 'singleton', 'singleton', active_methods
                )
                self.app_or_bp.add_url_rule(
                    rule, endpoint=legacy_singleton, view_func=wrapped,
                    methods=methods, strict_slashes=False,
                )
            self._register_extra_actions(view_suffix)
            return

        if self.pk_name in route_variables:
            raise ValueError(
                "pk_name '{}' already appears in url_prefix '{}'; ".format(
                    self.pk_name, self.url_prefix
                )
                + "use singleton=True for single-resource URLs."
            )

        base_rule = (self.url_prefix + view_suffix)
        collection_rule = base_rule
        if not collection_rule and not hasattr(self.app_or_bp, 'url_prefix'):
            collection_rule = '/'
        item_rule = '{}/<{}>'.format(base_rule.rstrip('/'), self.pk_name) if base_rule else '/<{}>'.format(self.pk_name)

        if 'GET' in self.methods:
            _add(collection_rule, CollectionView, 'list', 'collection', 'GET', 'collection')
        if 'POST' in self.methods:
            _add(collection_rule, CollectionView, 'create', 'collection', 'POST', 'collection')
        if 'GET' in self.methods:
            _add(item_rule, ItemView, 'retrieve', 'item', 'GET', 'detail')
        if 'PUT' in self.methods:
            _add(item_rule, ItemView, 'update', 'item', 'PUT', 'detail')
        if 'PATCH' in self.methods:
            _add(item_rule, ItemView, 'patch', 'item', 'PATCH', 'detail')
        if 'DELETE' in self.methods:
            _add(item_rule, ItemView, 'delete', 'item', 'DELETE', 'detail')
        _flush()
        # Legacy v1 endpoint aliases for production backward compatibility
        col_methods = self.methods.intersection({'GET', 'POST'})
        if col_methods:
            legacy_col = '{}_collection'.format(self.endpoint)
            wrapped, methods = _wrap(
                CollectionView, legacy_col, 'collection', 'collection', col_methods
            )
            self.app_or_bp.add_url_rule(
                collection_rule, endpoint=legacy_col, view_func=wrapped,
                methods=methods, strict_slashes=False,
            )
        item_methods = self.methods.intersection({'GET', 'PUT', 'PATCH', 'DELETE'})
        if item_methods:
            legacy_detail = '{}_detail'.format(self.endpoint)
            wrapped, methods = _wrap(
                ItemView, legacy_detail, 'item', 'detail', item_methods
            )
            self.app_or_bp.add_url_rule(
                item_rule, endpoint=legacy_detail, view_func=wrapped,
                methods=methods, strict_slashes=False,
            )
        self._register_extra_actions(view_suffix)

    def _wrap_view_errors(self, view_func, resource):
        """Wrap a ``MethodView`` view-func with the errors pipeline."""
        return self._json_errors(view_func, resource)

    def _make_extra_action_view(self, action_name, config, handler, detail, inherit=True):
        def view_func(**route_values):
            self._validate_view_args(route_values)

            # Determine whether we should query/load the DB item
            ITEM_PARAM_NAMES = {'item', 'instance', 'obj', 'record', 'model_instance'}
            has_item_param = False
            try:
                sig = inspect.signature(handler)
                for p in sig.parameters.values():
                    if p.name in ITEM_PARAM_NAMES:
                        has_item_param = True
                        break
            except (ValueError, TypeError):
                pass

            should_load_item = (
                config.get('load_item', False)
                or (detail is True)
                or has_item_param
            )

            primary = None
            if should_load_item:
                if self.singleton:
                    primary = self._query(route_values, request.method, 'singleton').first()
                    if primary is None and (detail is True or not config.get('allow_missing_item')):
                        return self._not_found_response(
                            ctx={'action': action_name, 'route_values': dict(route_values)},
                            message="{} not found".format(self.model.__name__),
                        )
                else:
                    pk_val = route_values.get(self.pk_name, route_values.get('id'))
                    if pk_val is None:
                        for k, v in route_values.items():
                            if k.endswith('_id') or k.endswith('_pk'):
                                pk_val = v
                                break
                    if pk_val is not None:
                        primary = self._query(route_values, request.method, 'item').filter(
                            self._get_column(self.pk_name) == pk_val
                        ).first()
                    else:
                        primary = self._query(route_values, request.method, 'item').first()

                    if primary is None and (detail is True or has_item_param):
                        return self._not_found_response(
                            ctx={'action': action_name, 'route_values': dict(route_values)},
                            message="{} not found".format(self.model.__name__),
                        )

            # Schema handling
            action_schema = config.get('schema')
            if action_schema is None and config.get('inherit_schema', False):
                action_schema = self.schema

            data = None
            if request.is_json or (request.method in ('POST', 'PUT', 'PATCH') and request.get_data()):
                raw_json = request.get_json(silent=True) or {}
                data = self._inject_view_args(raw_json, route_values)
                validate = config.get('validate', True if action_schema is not None else False)
                if validate and action_schema is not None:
                    coerced = _coerce_schema(action_schema)
                    try:
                        data = coerced.load(data)
                    except ValidationError as error:
                        if inherit:
                            return self._validation_response(
                                error,
                                ctx={
                                    'action': action_name,
                                    'route_values': dict(route_values),
                                    'schema': action_schema,
                                }
                            )
                        raise

            # Invoke handler with intelligent parameter binding
            result = self._invoke_extra_handler(
                handler, route_values, primary=primary, data=data, config=config, action_schema=action_schema
            )

            if self._is_flask_response(result):
                return result

            # Optional schema dumping for returned model/dict
            if action_schema is not None:
                coerced = _coerce_schema(action_schema)
                if isinstance(result, tuple) and len(result) >= 2:
                    body, status = result[0], result[1]
                    rest = result[2:]
                    if hasattr(body, '__table__') or isinstance(body, list):
                        body = coerced.dump(body, many=isinstance(body, list))
                    result = (body, status, *rest)
                elif hasattr(result, '__table__') or (isinstance(result, list) and result and hasattr(result[0], '__table__')):
                    result = coerced.dump(result, many=isinstance(result, list))

            if inherit:
                default_status = config.get('status', 200)
                return self._finalize_body(result, default_status)

            if isinstance(result, tuple):
                body = result[0]
                status = result[1]
                rest = result[2:]
                if isinstance(body, (dict, list)):
                    from flask import jsonify
                    return (jsonify(body), status, *rest)
                return result

            if isinstance(result, (dict, list)):
                from flask import jsonify
                return jsonify(result)

            return result

        return view_func

    def _invoke_extra_handler(
        self, handler, route_values, primary=None, data=None, config=None, action_schema=None
    ):
        config = config or {}
        try:
            sig = inspect.signature(handler)
            params = list(sig.parameters.values())
        except (ValueError, TypeError):
            return handler()

        if not params:
            return handler()

        has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)

        context_lookup = {
            'builder': self,
            'api_builder': self,
            'self_builder': self,
            'model': self.model,
            'session': self.session,
            'db': self.session,
            'db_session': self.session,
            'schema': action_schema,
            'request': request,
            'data': data,
            'payload': data,
            'route_values': dict(route_values),
        }
        if primary is not None:
            context_lookup.update({
                'item': primary,
                'instance': primary,
                'obj': primary,
                'record': primary,
                'model_instance': primary,
            })

        pk_val = route_values.get(self.pk_name) or route_values.get('id')
        if pk_val is None:
            for k, v in route_values.items():
                if k.endswith('_id') or k.endswith('_pk'):
                    pk_val = v
                    break

        call_args = []
        call_kwargs = {}
        matched_names = set()

        for p in params:
            if p.kind == inspect.Parameter.POSITIONAL_ONLY:
                name = p.name
                val = inspect.Parameter.empty
                if name in route_values:
                    val = route_values[name]
                elif name in context_lookup:
                    val = context_lookup[name]
                elif (name.endswith('_id') or name.endswith('_pk') or name in ('id', 'pk')) and pk_val is not None:
                    val = pk_val
                elif primary is not None and 'item' not in matched_names:
                    val = primary
                    matched_names.add('item')
                elif data is not None and 'data' not in matched_names:
                    val = data
                    matched_names.add('data')
                elif p.default is not inspect.Parameter.empty:
                    val = p.default
                call_args.append(val)
            elif p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD:
                name = p.name
                if name in route_values:
                    call_kwargs[name] = route_values[name]
                    matched_names.add(name)
                elif name in context_lookup:
                    call_kwargs[name] = context_lookup[name]
                    matched_names.add(name)
                elif (name.endswith('_id') or name.endswith('_pk') or name in ('id', 'pk')) and pk_val is not None:
                    call_kwargs[name] = pk_val
                    matched_names.add(name)
                elif p.default is not inspect.Parameter.empty:
                    continue
                else:
                    if primary is not None and 'item' not in matched_names:
                        call_kwargs[name] = primary
                        matched_names.add('item')
                    elif data is not None and 'data' not in matched_names:
                        call_kwargs[name] = data
                        matched_names.add('data')
            elif p.kind == inspect.Parameter.KEYWORD_ONLY:
                name = p.name
                if name in route_values:
                    call_kwargs[name] = route_values[name]
                elif name in context_lookup:
                    call_kwargs[name] = context_lookup[name]
                elif (name.endswith('_id') or name.endswith('_pk') or name in ('id', 'pk')) and pk_val is not None:
                    call_kwargs[name] = pk_val

        if has_var_keyword:
            for k, v in route_values.items():
                if k not in call_kwargs:
                    call_kwargs[k] = v

        return handler(*call_args, **call_kwargs)

    def _register_extra_actions(self, view_suffix=None):
        if not self.extra_actions:
            return
        for action_name, config in list(self.extra_actions.items()):
            self.register_action(action_name, config, view_suffix=view_suffix)

    def register_action(self, action_name, config, view_suffix=None):
        """Append an extra action/method view to this resource's route."""
        if callable(config):
            config = {'handler': config}
        elif not isinstance(config, dict):
            return
        handler = config.get('handler')
        if isinstance(handler, str):
            handler = getattr(self, handler, None)
        if not callable(handler):
            return

        if view_suffix is None:
            view_suffix = getattr(self, '_view_suffix', '')

        methods = [m.upper() for m in config.get('methods', ['POST'])]

        raw_path = config.get('path', config.get('url', config.get('rule')))
        pk_token = '<{}>'.format(self.pk_name)

        if raw_path is not None:
            path_str = str(raw_path).strip()
            if not path_str.startswith('/'):
                path_str = '/' + path_str
            rule = '{}{}{}'.format(self.url_prefix.rstrip('/'), view_suffix, path_str)
            detail = config.get('detail', None)
        else:
            detail = config.get('detail', False)
            sub_path = action_name.strip('/')
            if self.singleton or not detail:
                rule = '{}{}/{}'.format(self.url_prefix.rstrip('/'), view_suffix, sub_path)
            else:
                rule = '{}{}/<{}>/{}'.format(
                    self.url_prefix.rstrip('/'), view_suffix, self.pk_name, sub_path
                )

        if not rule.startswith('/'):
            rule = '/' + rule

        endpoint = config.get('endpoint')
        if not endpoint:
            endpoint = '{}_{}'.format(self.endpoint, action_name.replace('-', '_'))

        inherit = config.get('inherit', True)
        action_view = self._make_extra_action_view(
            action_name, config, handler, detail, inherit=inherit
        )

        action_decorators = config.get('decorators', ())
        if callable(action_decorators):
            action_decorators = (action_decorators,)
        for dec in reversed(action_decorators):
            action_view = dec(action_view)

        if inherit:
            final_view = self._decorate(
                self._json_errors(action_view, 'item' if detail else 'collection'),
                endpoint,
                'item' if detail else 'collection',
            )
        else:
            final_view = action_view

        self.app_or_bp.add_url_rule(
            rule,
            endpoint=endpoint,
            view_func=final_view,
            methods=sorted(methods),
            strict_slashes=False,
        )
        self.extra_actions[action_name] = config

    def action(
        self,
        name=None,
        path=None,
        methods=None,
        schema=None,
        detail=None,
        inherit=True,
        **kwargs
    ):
        """Append a custom action view to this resource's route.

        Can be used as a decorator on custom views:

            @products.action('publish', path='/<publish_id>/publish', methods=['POST', 'GET'])
            def publish(publish_id=None):
                if request.method == 'POST':
                    return {'status': 'published', 'id': publish_id}
                return {'status': 'draft', 'id': publish_id}
        """
        def decorator(fn):
            action_name = name or fn.__name__
            cfg = dict(kwargs)
            cfg.update({
                'handler': fn,
                'path': path,
                'methods': methods or ['POST'],
                'schema': schema,
                'detail': detail,
                'inherit': inherit,
            })
            self.register_action(action_name, cfg)
            return fn
        return decorator

    def route(
        self,
        path,
        methods=None,
        endpoint=None,
        schema=None,
        inherit=True,
        **kwargs
    ):
        """Append a custom route onto this builder's url_prefix.

        Can be used as a decorator:

            @products.route('/<publish_id>/publish', methods=['POST', 'GET'])
            def publish(publish_id=None):
                if request.method == 'POST':
                    return {'status': 'published', 'id': publish_id}
        """
        def decorator(fn):
            action_name = endpoint or fn.__name__
            cfg = dict(kwargs)
            cfg.update({
                'handler': fn,
                'path': path,
                'methods': methods or ['GET'],
                'endpoint': endpoint,
                'schema': schema,
                'inherit': inherit,
            })
            self.register_action(action_name, cfg)
            return fn
        return decorator

    # -- action / schema / response / error plumbing (delegates) ----------

    @staticmethod
    def _canonical_action(method, resource):
        """Map ``(HTTP method, resource)`` to a semantic action (see helpers)."""
        return canonical_action(method, resource)

    def _resolve_action_value(self, mapping, action, resource, method, extra_candidates=()):
        """Resolve ``mapping`` for ``(action, resource, method)``.

        Candidate order (most specific first): extra candidates (e.g. 'paginated'),
        ``METHOD_resource``, ``METHOD_action``, ``action_resource``, ``action``,
        ``resource``, ``METHOD``, endpoint name. ``PATCH`` with no ``patch`` entry falls
        back to the equivalent ``update`` candidates.
        """
        if not mapping:
            return None
        found = _resolve_action_value(
            mapping, action, resource, method, self.endpoint, extra_candidates
        )
        if found is None and (action or '').lower() == 'patch':
            found = _resolve_action_value(
                mapping, 'update', resource, method, self.endpoint, extra_candidates
            )
        return found

    def _override_for(self, action, resource, method):
        """Return the override handler for an action (or ``None``)."""
        return self._resolve_action_value(self.overrides, action, resource, method)

    def _merged_responses(self):
        """Global ``FlaskUtility`` responses merged under per-builder ones."""
        merged = {}
        for key, value in dict(resolve_responses() or {}).items():
            merged[_normalize_action_key(key)] = value
        for key, value in dict(self.responses or {}).items():
            merged[_normalize_action_key(key)] = value
        return merged

    def _has_response_for(self, action, resource, method, extra_candidates=()):
        """True when a ``responses`` entry exists (matters for ``delete``)."""
        return (
            self._resolve_action_value(
                self._merged_responses(), action, resource, method, extra_candidates
            )
            is not None
        )

    def _schema_for(self, action, resource, method):
        """Return the schema instance for an action (default ``schema``)."""
        raw = self._resolve_action_value(self.schemas, action, resource, method)
        if raw is None:
            return self.schema
        coerced = _coerce_schema(raw)
        return coerced if coerced is not None else self.schema

    def _merged_errors(self):
        """Global ``FlaskUtility`` errors merged under per-builder ones."""
        merged = {}
        for key, value in dict(resolve_errors() or {}).items():
            merged[_normalize_error_key(key)] = value
        for key, value in dict(self.errors or {}).items():
            merged[_normalize_error_key(key)] = value
        return merged

    def _lookup_error_handler(self, code, aliases=()):
        """Return ``(handler, matched_key)`` for a status code + aliases."""
        handlers = self._merged_errors()
        if code in handlers:
            return handlers[code], code
        for alias in aliases:
            normalized = _normalize_error_key(alias)
            if normalized in handlers:
                return handlers[normalized], normalized
        if 'http' in handlers and code is not None:
            return handlers['http'], 'http'
        return None, None

    @staticmethod
    def _is_flask_response(value):
        return is_flask_response(value)

    @staticmethod
    def _is_headers(value):
        """True for Flask-style headers (see helpers)."""
        return is_headers(value)

    @staticmethod
    def _default_status_for(action):
        """Default HTTP status per semantic action (see helpers)."""
        return default_status_for(action)

    @staticmethod
    def _apply_headers(response, headers):
        """Apply headers (see helpers)."""
        from .api_helpers import apply_headers as _apply
        return _apply(response, headers)

    def _build_response(self, body, status, headers=None):
        """Builder-owned response construction (see helpers)."""
        return build_response(body, status, headers)

    def _normalize_handler_result(self, result, default_status):
        """Normalise handler returns (see helpers)."""
        return normalize_handler_result(result, default_status)

    def _finalize_body(self, body, default_status):
        """Wrap a handler return into a Flask response (builder jsonifies)."""
        return normalize_handler_result(body, default_status)

    def _call_with_data_ctx(self, handler, data, ctx):
        """Call ``handler(data[, ctx])`` supporting the short form."""
        return call_with_data_ctx(handler, data, ctx)

    def _call_error_handler(self, handler, error, ctx):
        """Call ``handler(error[, ctx])`` supporting short forms."""
        return call_error_handler(handler, error, ctx)

    def _invoke_override(self, handler, primary, data, route_values, extra=None):
        """Invoke an override with flexible by-name or positional binding.

        By-name parameters are filled from ``query/item/data/route_values/
        request/builder/api_builder/action/schema/session``. Otherwise the
        leading positional prefix ``(primary, data, route_values, request,
        builder)`` is used (same convention as :meth:`_run_hooks`).
        """
        extra = extra or {}
        ctx_map = {
            'query': primary,
            'item': primary,
            'data': data,
            'route_values': route_values,
            'route': route_values,
            'request': request,
            'req': request,
            'builder': self,
            'api_builder': self,
            'action': extra.get('action'),
            'schema': extra.get('schema', self.schema),
            'session': self.session,
        }
        try:
            sig = signature(handler)
        except (TypeError, ValueError):
            sig = None
        if sig is not None:
            params = list(sig.parameters.values())
            accepts_var_positional = any(
                p.kind == Parameter.VAR_POSITIONAL for p in params
            )
            if not accepts_var_positional:
                kwargs = {}
                feasible = True
                for param in params:
                    if param.kind in (
                        Parameter.VAR_KEYWORD, Parameter.VAR_POSITIONAL
                    ):
                        continue
                    if param.name in ctx_map:
                        kwargs[param.name] = ctx_map[param.name]
                    elif param.default is Parameter.empty:
                        feasible = False
                        break
                if feasible:
                    try:
                        sig.bind(**kwargs)
                    except (TypeError, ValueError):
                        pass
                    else:
                        return handler(**kwargs)
        positional = (primary, data, route_values, request, self)
        try:
            handler_sig = signature(handler)
        except (TypeError, ValueError):
            return handler(*positional)
        try:
            handler_sig.bind(*positional)
        except TypeError:
            for count in range(len(positional) - 1, 0, -1):
                try:
                    handler_sig.bind(*positional[:count])
                except TypeError:
                    continue
                return handler(*positional[:count])
            try:
                handler_sig.bind()
            except TypeError:
                return handler(positional[0])
            return handler()
        return handler(*positional)

    def _json_errors(self, view_func, resource='item'):
        """Wrap view with builder error pipeline (see api_decorators)."""
        return make_json_errors(self, view_func, resource)

    def _validation_response(self, error, ctx=None):
        """Validation payload — conventional ``422``, ``400`` fallback.

        Resolution: explicit ``422`` (or ``'validation'`` / ``'schema'`` /
        ``'unprocessable_entity'`` aliases) first, then ``400``. Default
        status is ``422``; only a ``400``-keyed handler forces ``400``.
        Handlers may still override via ``(body, status[, headers])``.
        """
        ctx = dict(ctx or {})
        ctx.setdefault('action', '')
        ctx.setdefault('request', request)
        ctx.setdefault('route_values', {})
        ctx.setdefault('builder', self)
        ctx.setdefault('error_kind', 'validation')

        # Conventional 422 first (code + all validation aliases incl. schema),
        # then 400 fallback for legacy clients.
        handler, matched = self._lookup_error_handler(
            422, ('validation', 'schema', 'schema_error', 'schema_errors',
                  'marshmallow', 'unprocessable_entity', 'unprocessable')
        )
        if handler is None:
            handler, matched = self._lookup_error_handler(400, ('validation',))

        # Default status: 422 conventional; 400 only when matched via 400 key.
        if matched == 400:
            ctx['status'] = 400
        else:
            ctx['status'] = 422

        if handler is not None:
            try:
                result = self._call_error_handler(handler, error, ctx)
            except Exception:
                pass
            else:
                return self._finalize_body(result, ctx['status'])
        return jsonify({
            'message': 'Validation Error',
            'errors': error.messages,
        }), ctx['status']

    def _not_found_response(self, ctx=None, message='Resource not found'):
        """``404`` payload (customisable via ``errors[404/'not_found']``)."""
        ctx = dict(ctx or {})
        ctx.setdefault('action', '')
        ctx.setdefault('status', 404)
        ctx.setdefault('request', request)
        ctx.setdefault('route_values', {})
        ctx.setdefault('builder', self)
        ctx.setdefault('error_kind', 'not_found')
        handler, _ = self._lookup_error_handler(
            404, ('not_found', 'not-found', 'notfound')
        )
        if handler is not None:
            error = HTTPException(description=message)
            error.code = 404
            try:
                result = self._call_error_handler(handler, error, ctx)
            except Exception:
                pass
            else:
                return self._finalize_body(result, ctx['status'])
        return jsonify(message=message), 404

    def _generic_error_response(self, error, ctx=None):
        """``400`` fallback payload (customisable via ``errors``)."""
        ctx = dict(ctx or {})
        ctx.setdefault('action', '')
        ctx.setdefault('status', 400)
        ctx.setdefault('request', request)
        ctx.setdefault('route_values', {})
        ctx.setdefault('builder', self)
        ctx.setdefault('error_kind', 'generic')
        handler, _ = self._lookup_error_handler(400, ('generic',))
        if handler is not None:
            try:
                result = self._call_error_handler(handler, error, ctx)
            except Exception:
                pass
            else:
                return self._finalize_body(result, ctx['status'])
        return jsonify(message=str(error)), 400

    def _decorate(self, view_func, method, resource_name):
        """Apply configured decorators (see api_decorators)."""
        return apply_decorators(self, view_func, method, resource_name)

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
                # New-style: never touch legacy ``model.query``.
                found = first_by_column(self.session, reference_model,
                                        column, reference_value)

            if found is None:
                abort(404, description='{} not found'.format(
                    reference_model.__name__
                ))

    def _ensure_adapter(self, query):
        """Coerce ``query_override`` results to a ``QueryAdapter``.

        Legacy ``Query`` objects (``filter/all/first/paginate``) pass
        through untouched; raw ``select()`` statements are wrapped so the
        rest of the builder always sees the same API.
        """
        return ensure_adapter(self.model, self.session, query)

    def _query(self, route_values, operation=None, resource_name=None):
        # New-style primary path (SQLAlchemy 2.0 ``select`` via adapter).
        query = base_query(self.model, self.session)
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
            query = self._ensure_adapter(query)

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
        """Dump with the default schema (backward-compatible shortcut)."""
        return self.schema.dump(value, many=many)

    def _load(self, data, instance=None, partial=False):
        """Load with the default schema (backward-compatible shortcut)."""
        if instance is None:
            return self.schema.load(data)
        try:
            return self.schema.load(data, instance=instance, partial=partial)
        except TypeError:
            return self.schema.load(data, partial=partial)

    def _serialize_for(self, action, value, many=False, resource='item', method=None):
        """Dump ``value`` with the schema resolved for ``action``."""
        schema = self._schema_for(action, resource, method or request.method)
        return schema.dump(value, many=many)

    def _load_for(self, action, data, instance=None, partial=False,
                  resource='item', method=None):
        """Load ``data`` with the schema resolved for ``action``."""
        schema = self._schema_for(action, resource, method or request.method)
        if instance is None:
            return schema.load(data)
        try:
            return schema.load(data, instance=instance, partial=partial)
        except TypeError:
            return schema.load(data, partial=partial)

    def _apply_response(self, action, data, default_status, route_values,
                        resource='item', method=None, extra_candidates=()):
        """Apply ``responses[action]`` shaping or the plain default.

        ``responses`` handlers return plain ``data`` / ``(data, status)`` /
        ``(data, status, headers)``; the builder owns ``jsonify``.
        """
        method = method or request.method
        handler = self._resolve_action_value(
            self._merged_responses(), action, resource, method, extra_candidates
        )
        ctx = {
            'action': action,
            'status': default_status,
            'request': request,
            'route_values': dict(route_values or {}),
            'builder': self,
        }
        if handler is None:
            if data is None and default_status == 204:
                return self._build_response('', 204)
            return self._build_response(data, default_status)
        result = self._call_with_data_ctx(handler, data, ctx)
        return self._normalize_handler_result(result, default_status)

    def _run_override(self, action, primary, data, route_values,
                      resource='item', method=None):
        """Run ``overrides[action]``; return ``(handled, response)``.

        ``handled`` is ``False`` when no override is configured. Otherwise
        the handler may return plain ``data`` / ``(data, status)`` /
        ``(data, status, headers)`` / ``(data, headers)`` or a Flask
        ``Response`` — the builder owns ``jsonify``/status/headers::

            def audit_create(data, builder):
                item = builder.schema.load(data)
                builder.session.add(item)
                builder.session.commit()
                return builder.schema.dump(item), 201

            def with_headers(query, builder):
                return {'items': [...]}, 200, {'X-Total': '3'}

        ``ValidationError``/generic failures are converted through the
        ``errors`` pipeline with a session rollback, mirroring the default
        paths.
        """
        method = method or request.method
        handler = self._override_for(action, resource, method)
        if handler is None:
            return False, None
        default_status = self._default_status_for(action)
        ctx_base = {
            'action': action,
            'status': default_status,
            'request': request,
            'route_values': dict(route_values or {}),
            'builder': self,
        }
        schema = self._schema_for(action, resource, method)
        extra = {'action': action, 'schema': schema}
        try:
            result = self._invoke_override(
                handler, primary, data, dict(route_values or {}), extra
            )
        except ValidationError as error:
            self.session.rollback()
            ctx_base['error_kind'] = 'validation'
            return True, self._validation_response(error, ctx_base)
        except HTTPException:
            raise
        except Exception as error:
            self.session.rollback()
            ctx_base['error_kind'] = 'generic'
            return True, self._generic_error_response(error, ctx_base)
        return True, self._normalize_handler_result(result, default_status)

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

    # -- MethodView units (logic; views in api_views.py dispatch here) -------

    def do_list(self, route_values=None, method='GET'):
        """List collection (used by ``CollectionView.get``)."""
        route_values = dict(route_values or {})
        self._validate_view_args(route_values)
        action = 'list'
        query = self._query(route_values, method, 'collection')
        handled, response = self._run_override(
            action, query, None, route_values,
            resource='collection', method=method,
        )
        if handled:
            return response
        if not self.paginate:
            dumped = self._serialize_for(
                action, query.all(), many=True,
                resource='collection', method=method,
            )
            return self._apply_response(
                action, dumped, 200, route_values,
                resource='collection', method=method,
                extra_candidates=('collection', 'unpaginated'),
            )

        page = max(1, request.args.get('page', default=1, type=int))
        per_page = min(
            self.max_per_page,
            max(1, request.args.get('per_page', default=self.per_page, type=int)),
        )
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        dumped = {
            'items': self._serialize_for(
                action, pagination.items, many=True,
                resource='collection', method=method,
            ),
            'page': pagination.page,
            'per_page': pagination.per_page,
            'pages': pagination.pages,
            'total': pagination.total,
        }
        return self._apply_response(
            action, dumped, 200, route_values,
            resource='collection', method=method,
            extra_candidates=('paginated', 'list_paginated'),
        )

    def do_create(self, route_values=None, method='POST'):
        """Create resource (used by ``CollectionView.post``)."""
        route_values = dict(route_values or {})
        self._validate_view_args(route_values)
        action = 'create'
        data = self._inject_view_args(
            request.get_json(silent=True) or {}, route_values
        )
        handled, response = self._run_override(
            action, None, data, route_values,
            resource='collection', method=method,
        )
        if handled:
            return response
        try:
            item = self._load_for(
                action, data, resource='collection', method=method
            )
            if isinstance(item, dict):
                item = self.model(**item)
            self._run_hooks('before_create', item, data, route_values)
            self.session.add(item)
            self.session.commit()
            self._run_hooks('after_create', item, data, route_values)
        except ValidationError as error:
            self.session.rollback()
            return self._validation_response(error, {
                'action': action, 'status': 400, 'request': request,
                'route_values': dict(route_values), 'builder': self,
                'error_kind': 'validation',
            })
        except Exception as error:
            self.session.rollback()
            return self._generic_error_response(error, {
                'action': action, 'status': 400, 'request': request,
                'route_values': dict(route_values), 'builder': self,
                'error_kind': 'generic',
            })
        dumped = self._serialize_for(
            action, item, resource='collection', method=method
        )
        return self._apply_response(
            action, dumped, 201, route_values,
            resource='item', method=method,
        )

    def _collection(self, **route_values):
        """Backward-compatible dispatcher (kept; views call ``do_*``)."""
        if request.method == 'POST':
            return self.do_create(route_values, method='POST')
        return self.do_list(route_values, method=request.method or 'GET')

    def _fetch_item(self, route_values, method):
        """Fetch item or return ``(None, not_found_response)`` tuple."""
        item = self._query(route_values, method, 'item').filter(
            self._get_column(self.pk_name) == route_values[self.pk_name]
        ).first()
        return item

    def do_retrieve(self, route_values=None, method='GET'):
        """Retrieve one item (used by ``ItemView.get``)."""
        route_values = dict(route_values or {})
        self._validate_view_args(route_values)
        item = self._fetch_item(route_values, method)
        if item is None:
            return self._not_found_response({
                'action': 'retrieve',
                'status': 404, 'request': request,
                'route_values': dict(route_values), 'builder': self,
                'error_kind': 'not_found',
            })
        handled, response = self._run_override(
            'retrieve', item, None, route_values,
            resource='item', method=method,
        )
        if handled:
            return response
        dumped = self._serialize_for(
            'retrieve', item, resource='item', method=method
        )
        return self._apply_response(
            'retrieve', dumped, 200, route_values,
            resource='item', method=method,
        )

    def do_update(self, route_values=None, method=None, partial=True):
        """Update one item (used by ``ItemView.put/patch``)."""
        route_values = dict(route_values or {})
        self._validate_view_args(route_values)
        method = method or request.method or 'PUT'
        lookup_action = 'patch' if method == 'PATCH' else 'update'
        item = self._fetch_item(route_values, method)
        if item is None:
            return self._not_found_response({
                'action': lookup_action,
                'status': 404, 'request': request,
                'route_values': dict(route_values), 'builder': self,
                'error_kind': 'not_found',
            })
        data = self._inject_view_args(
            request.get_json(silent=True) or {}, route_values
        )
        handled, response = self._run_override(
            lookup_action, item, data, route_values,
            resource='item', method=method,
        )
        if handled:
            return response
        try:
            loaded = self._load_for(
                lookup_action, data, instance=item, partial=True,
                resource='item', method=method,
            )
            if isinstance(loaded, dict):
                for key, value in loaded.items():
                    setattr(item, key, value)
            self._run_hooks('before_update', item, data, route_values)
            self.session.commit()
            self._run_hooks('after_update', item, data, route_values)
        except ValidationError as error:
            self.session.rollback()
            return self._validation_response(error, {
                'action': lookup_action, 'status': 400, 'request': request,
                'route_values': dict(route_values), 'builder': self,
                'error_kind': 'validation',
            })
        except Exception as error:
            self.session.rollback()
            return self._generic_error_response(error, {
                'action': lookup_action, 'status': 400, 'request': request,
                'route_values': dict(route_values), 'builder': self,
                'error_kind': 'generic',
            })
        dumped = self._serialize_for(
            lookup_action, item, resource='item', method=method
        )
        return self._apply_response(
            lookup_action, dumped, 200, route_values,
            resource='item', method=method,
        )

    def do_delete(self, route_values=None, method='DELETE'):
        """Delete one item (used by ``ItemView.delete``)."""
        route_values = dict(route_values or {})
        self._validate_view_args(route_values)
        item = self._fetch_item(route_values, method)
        if item is None:
            return self._not_found_response({
                'action': 'delete',
                'status': 404, 'request': request,
                'route_values': dict(route_values), 'builder': self,
                'error_kind': 'not_found',
            })
        handled, response = self._run_override(
            'delete', item, None, route_values,
            resource='item', method=method,
        )
        if handled:
            return response
        try:
            self._run_hooks('before_delete', item, None, route_values)
            self.session.delete(item)
            self.session.commit()
            self._run_hooks('after_delete', item, None, route_values)
        except ValidationError as error:
            self.session.rollback()
            return self._validation_response(error, {
                'action': 'delete', 'status': 400, 'request': request,
                'route_values': dict(route_values), 'builder': self,
                'error_kind': 'validation',
            })
        except Exception as error:
            self.session.rollback()
            return self._generic_error_response(error, {
                'action': 'delete', 'status': 400, 'request': request,
                'route_values': dict(route_values), 'builder': self,
                'error_kind': 'generic',
            })
        if self._has_response_for('delete', 'item', method):
            return self._apply_response(
                'delete', None, 200, route_values,
                resource='item', method=method,
            )
        return '', 204

    def _item(self, **route_values):
        """Backward-compatible dispatcher (kept; views call ``do_*``)."""
        method = request.method
        if method == 'GET':
            return self.do_retrieve(route_values, method=method)
        if method == 'DELETE':
            return self.do_delete(route_values, method=method)
        if method == 'PATCH':
            return self.do_update(route_values, method=method, partial=True)
        return self.do_update(route_values, method=method, partial=False)

    def do_singleton_retrieve(self, route_values=None, method='GET'):
        """Retrieve singleton (used by ``SingletonView.get``)."""
        route_values = dict(route_values or {})
        self._validate_view_args(route_values)
        item = self._query(route_values, method, 'singleton').first()
        if item is None:
            return self._not_found_response({
                'action': 'retrieve', 'status': 404, 'request': request,
                'route_values': dict(route_values), 'builder': self,
                'error_kind': 'not_found',
            })
        handled, response = self._run_override(
            'retrieve', item, None, route_values,
            resource='singleton', method=method,
        )
        if handled:
            return response
        dumped = self._serialize_for(
            'retrieve', item, resource='singleton', method=method
        )
        return self._apply_response(
            'retrieve', dumped, 200, route_values,
            resource='singleton', method=method,
        )

    def do_singleton_delete(self, route_values=None, method='DELETE'):
        """Delete singleton (used by ``SingletonView.delete``)."""
        route_values = dict(route_values or {})
        self._validate_view_args(route_values)
        item = self._query(route_values, method, 'singleton').first()
        if item is None:
            return self._not_found_response({
                'action': 'delete', 'status': 404, 'request': request,
                'route_values': dict(route_values), 'builder': self,
                'error_kind': 'not_found',
            })
        handled, response = self._run_override(
            'delete', item, None, route_values,
            resource='singleton', method=method,
        )
        if handled:
            return response
        try:
            self._run_hooks('before_delete', item, None, route_values)
            self.session.delete(item)
            self.session.commit()
            self._run_hooks('after_delete', item, None, route_values)
        except ValidationError as error:
            self.session.rollback()
            return self._validation_response(error, {
                'action': 'delete', 'status': 400, 'request': request,
                'route_values': dict(route_values), 'builder': self,
                'error_kind': 'validation',
            })
        except Exception as error:
            self.session.rollback()
            return self._generic_error_response(error, {
                'action': 'delete', 'status': 400, 'request': request,
                'route_values': dict(route_values), 'builder': self,
                'error_kind': 'generic',
            })
        if self._has_response_for('delete', 'singleton', method):
            return self._apply_response(
                'delete', None, 200, route_values,
                resource='singleton', method=method,
            )
        return '', 204

    def do_singleton_write(self, route_values=None, method=None):
        """Upsert singleton (used by ``SingletonView.post/put/patch``)."""
        route_values = dict(route_values or {})
        self._validate_view_args(route_values)
        method = method or request.method or 'POST'
        item = self._query(route_values, method, 'singleton').first()
        # Upsert: missing row behaves as ``create`` (201), present row as
        # ``update``/``patch`` (200).
        if item is None:
            action = 'create'
        elif method == 'PATCH':
            action = 'patch'
        else:
            action = 'update'
        data = self._inject_view_args(
            request.get_json(silent=True) or {}, route_values
        )
        handled, response = self._run_override(
            action, item, data, route_values,
            resource='singleton', method=method,
        )
        if handled:
            return response
        try:
            if item is None:
                item = self._load_for(
                    action, data, resource='singleton', method=method
                )
                if isinstance(item, dict):
                    item = self.model(**item)
                self._run_hooks('before_create', item, data, route_values)
                self.session.add(item)
                self.session.commit()
                self._run_hooks('after_create', item, data, route_values)
                dumped = self._serialize_for(
                    action, item, resource='singleton', method=method
                )
                return self._apply_response(
                    action, dumped, 201, route_values,
                    resource='singleton', method=method,
                )
            loaded = self._load_for(
                action, data, instance=item, partial=True,
                resource='singleton', method=method,
            )
            if isinstance(loaded, dict):
                for key, value in loaded.items():
                    setattr(item, key, value)
            self._run_hooks('before_update', item, data, route_values)
            self.session.commit()
            self._run_hooks('after_update', item, data, route_values)
        except ValidationError as error:
            self.session.rollback()
            return self._validation_response(error, {
                'action': action, 'status': 400, 'request': request,
                'route_values': dict(route_values), 'builder': self,
                'error_kind': 'validation',
            })
        except Exception as error:
            self.session.rollback()
            return self._generic_error_response(error, {
                'action': action, 'status': 400, 'request': request,
                'route_values': dict(route_values), 'builder': self,
                'error_kind': 'generic',
            })
        dumped = self._serialize_for(
            action, item, resource='singleton', method=method
        )
        return self._apply_response(
            action, dumped, 200, route_values,
            resource='singleton', method=method,
        )

    def _singleton(self, **route_values):
        """Backward-compatible dispatcher (kept; views call ``do_singleton_*``).

        ``GET`` returns the row or ``404``. ``POST``, ``PUT`` and ``PATCH``
        create the row when missing (``201``) or update it when present
        (``200``). ``DELETE`` removes it or returns ``404``.
        """
        method = request.method
        if method == 'GET':
            return self.do_singleton_retrieve(route_values, method=method)
        if method == 'DELETE':
            return self.do_singleton_delete(route_values, method=method)
        return self.do_singleton_write(route_values, method=method)