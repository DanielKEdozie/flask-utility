"""View decorators for :class:`ApiBuilder`.

Extracted from the builder so route wrapping (error shaping + custom
decorators) lives in one testable place. Both functions take the builder
explicitly — no imports from ``api_builder`` (avoids cycles).
"""
from functools import wraps

from flask import jsonify, request
from marshmallow import ValidationError
from werkzeug.exceptions import HTTPException


def make_json_errors(builder, view_func, resource='item'):
    """Wrap ``view_func`` with the builder's ``errors`` pipeline."""
    from .api_helpers import canonical_action

    @wraps(view_func)
    def wrapped(*args, **kwargs):
        try:
            return view_func(*args, **kwargs)
        except ValidationError as error:
            action = canonical_action(request.method, resource)
            ctx = {
                'action': action,
                'request': request,
                'route_values': dict(kwargs),
                'builder': builder,
                'error_kind': 'validation',
            }
            return builder._validation_response(error, ctx)
        except HTTPException as error:
            action = canonical_action(request.method, resource)
            ctx = {
                'action': action,
                'status': error.code or 500,
                'request': request,
                'route_values': dict(kwargs),
                'builder': builder,
                'error_kind': 'http',
            }
            handler, _ = builder._lookup_error_handler(error.code, ('http',))
            if handler is not None:
                try:
                    result = builder._call_error_handler(handler, error, ctx)
                except Exception:
                    pass
                else:
                    return builder._finalize_body(result, ctx['status'])
            return jsonify({
                'message': error.description,
                'status': error.code,
            }), error.code

    return wrapped


def apply_decorators(builder, view_func, method, resource_name):
    """Apply ``builder.decorators`` for ``method``/``resource``/endpoint."""
    decorators = []
    decorators.extend(builder.decorators.get(resource_name, ()))
    decorators.extend(builder.decorators.get(builder.endpoint, ()))
    method_decorators = ()
    if method.upper() in {'GET', 'POST', 'PUT', 'DELETE', 'PATCH'}:
        decorators.extend(builder.decorators.get(method.upper(), ()))
        decorators.extend(builder.decorators.get(method.lower(), ()))
    else:
        base_view = view_func
        method_decorators = tuple(builder.decorators.get('GET', ()))
        method_decorators += tuple(builder.decorators.get('get', ()))
        method_decorators += tuple(builder.decorators.get('POST', ()))
        method_decorators += tuple(builder.decorators.get('post', ()))
        method_decorators += tuple(builder.decorators.get('PUT', ()))
        method_decorators += tuple(builder.decorators.get('put', ()))
        method_decorators += tuple(builder.decorators.get('DELETE', ()))
        method_decorators += tuple(builder.decorators.get('delete', ()))
        method_decorators += tuple(builder.decorators.get('PATCH', ()))
        method_decorators += tuple(builder.decorators.get('patch', ()))

        def dispatch(*args, **kwargs):
            selected = []
            for decorator_method in (request.method, request.method.lower()):
                selected.extend(builder.decorators.get(decorator_method, ()))
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
