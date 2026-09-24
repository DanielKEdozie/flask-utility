"""Pure helpers for :class:`ApiBuilder` (no Flask request context required).

Kept free of ``ApiBuilder`` imports so both ``flask_utility`` and the
standalone ``flask-api-builder`` package can share the logic. Functions
that build Flask responses import Flask lazily inside the call.
"""
from flask import jsonify, make_response


#: Canonical semantic actions. ``update`` covers ``PUT`` and — unless a
#: dedicated ``patch`` entry exists — ``PATCH``.
CANONICAL_ACTIONS = ('list', 'create', 'retrieve', 'update', 'patch', 'delete')

#: String aliases accepted for ``errors`` keys (normalised to lower-case
#: with ``-``/space converted to ``_`` before lookup).
ERROR_ALIAS_VARIANTS = {
    'validation': {'validation', 'schema', 'schema_error', 'schema_errors',
                   'marshmallow', 'unprocessable_entity', 'unprocessable'},
    'not_found': {'not_found', 'not-found', 'notfound'},
    'generic': {'generic'},
    'http': {'http'},
}

# Backwards-compatible private aliases (imported by older code/tests).
_CANONICAL_ACTIONS = CANONICAL_ACTIONS
_ERROR_ALIAS_VARIANTS = ERROR_ALIAS_VARIANTS


def normalize_error_key(key):
    """Normalise an ``errors`` mapping key to ``int`` or alias string."""
    if isinstance(key, int):
        return key
    if isinstance(key, str):
        text = key.strip().lower().replace('-', '_').replace(' ', '_')
        if text.lstrip('-').isdigit():
            try:
                return int(text)
            except ValueError:
                pass
        if text in ('unprocessable_entity', 'unprocessable'):
            return 422
        for canonical, variants in ERROR_ALIAS_VARIANTS.items():
            if text in variants:
                return canonical
        return text
    return key


def normalize_action_key(key):
    """Normalise an action/resource/method mapping key for lookup."""
    if isinstance(key, str):
        return key.strip().lower()
    return key


def coerce_schema(value):
    """Return a Marshmallow schema *instance* for a flexible reference."""
    if value is None:
        return None
    # SchemaBuilder (has ``.schema`` + ``__call__`` but no ``dump``).
    if (
        hasattr(value, 'schema')
        and callable(getattr(value, '__call__', None))
        and not hasattr(value, 'dump')
    ):
        inner = getattr(value, 'schema')
        return inner() if isinstance(inner, type) else inner
    if isinstance(value, type):
        return value()
    return value


def action_candidates(action, resource, method, endpoint, extra_candidates=()):
    """Ordered lookup keys for action-scoped mappings (most specific first)."""
    method_upper = (method or '').upper()
    method_lower = (method or '').lower()
    resource_norm = (resource or '').lower()
    if resource_norm == 'detail':
        resource_norm = 'item'
    action_norm = (action or '').lower()
    endpoint_norm = (endpoint or '')
    candidates = list(extra_candidates or ())
    candidates.extend([
        '{}_{}'.format(method_upper, resource_norm),
        '{}_{}'.format(method_lower, resource_norm),
        '{}_{}'.format(method_upper, action_norm),
        '{}_{}'.format(method_lower, action_norm),
        '{}_{}'.format(action_norm, resource_norm),
        action_norm,
        resource_norm,
        method_upper,
        method_lower,
    ])
    if endpoint_norm:
        candidates.append(endpoint_norm)
        if endpoint_norm.lower() != endpoint_norm:
            candidates.append(endpoint_norm.lower())
    # Drop empties like ``'_'`` when method/action is missing.
    return [c for c in candidates if c and str(c).strip('_')]


def resolve_action_value(mapping, action, resource, method, endpoint,
                         extra_candidates=()):
    """Return the first mapping value matching the candidate chain."""
    if not mapping:
        return None
    normalized = {}
    for key, value in mapping.items():
        normalized[normalize_action_key(key)] = value
    for candidate in action_candidates(action, resource, method, endpoint,
                                       extra_candidates):
        lookup = normalize_action_key(candidate)
        if lookup in normalized:
            return normalized[lookup]
    return None


def canonical_action(method, resource):
    """Map ``(HTTP method, resource)`` to a semantic action."""
    normalized = (resource or '').lower()
    if normalized == 'detail':
        normalized = 'item'
    upper = (method or '').upper()
    if normalized == 'collection':
        if upper == 'GET':
            return 'list'
        if upper == 'POST':
            return 'create'
    elif normalized in ('item', 'singleton'):
        if upper == 'GET':
            return 'retrieve'
        if upper in ('PUT', 'PATCH', 'POST'):
            return 'update' if upper != 'POST' else 'create'
        if upper == 'DELETE':
            return 'delete'
    return upper.lower() if upper else ''


def default_status_for(action):
    """Default HTTP status per semantic action."""
    normalized = (action or '').lower()
    if normalized == 'create':
        return 201
    if normalized == 'delete':
        return 204
    return 200


def is_flask_response(value):
    return hasattr(value, 'status_code') and hasattr(value, 'get_data')


def is_headers(value):
    """True for Flask-style headers (dict or list of ``(k, v)`` pairs)."""
    if isinstance(value, dict):
        return True
    if isinstance(value, (list, tuple)) and value:
        return all(
            isinstance(pair, (list, tuple)) and len(pair) == 2
            for pair in value
        )
    return False


def apply_headers(response, headers):
    """Apply ``headers`` dict/pairs to a Flask response (best-effort)."""
    if not headers:
        return response
    try:
        items = headers.items() if isinstance(headers, dict) else headers
        for key, value in items:
            response.headers[key] = value
    except Exception:
        pass
    return response


def build_response(body, status, headers=None):
    """Builder-owned response construction (Flask view conventions)."""
    if is_flask_response(body):
        if status is not None:
            try:
                body.status_code = status
            except Exception:
                pass
        return apply_headers(body, headers)
    if body is None and status == 204:
        return apply_headers(make_response('', status), headers)
    if isinstance(body, (dict, list)):
        response = jsonify(body)
    elif isinstance(body, (str, bytes)):
        response = make_response(body, status or 200)
        return apply_headers(response, headers)
    else:
        response = jsonify(body)
    if status is not None:
        try:
            response.status_code = status
        except Exception:
            pass
    return apply_headers(response, headers)


def normalize_handler_result(result, default_status):
    """Normalise ``data`` / ``(data, status)`` / ``(data, headers)`` / \
    ``(data, status, headers)`` / ``Response`` into a Flask response."""
    if is_flask_response(result):
        return result
    if isinstance(result, tuple):
        if len(result) == 3:
            body, status, headers = result
            if isinstance(status, (int, str)) and is_headers(headers):
                return build_response(body, status, headers)
            return build_response(result, default_status)
        if len(result) == 2:
            body, second = result
            if isinstance(second, (int, str)) and not isinstance(second, bool):
                if is_headers(body) is False:
                    return build_response(body, second)
            if is_headers(second):
                return build_response(body, default_status, second)
            if isinstance(second, (int, str)):
                return build_response(body, second)
            return build_response(result, default_status)
    return build_response(result, default_status)


def call_with_data_ctx(handler, data, ctx):
    """Call ``handler(data[, ctx])`` supporting the short form."""
    from inspect import signature
    try:
        sig = signature(handler)
    except (TypeError, ValueError):
        return handler(data, ctx)
    try:
        sig.bind(data, ctx)
    except (TypeError, ValueError):
        try:
            sig.bind(data)
        except (TypeError, ValueError):
            return handler(data, ctx)
        return handler(data)
    return handler(data, ctx)


def call_error_handler(handler, error, ctx):
    """Call ``handler(error[, ctx])`` supporting short forms."""
    from inspect import signature
    try:
        sig = signature(handler)
    except (TypeError, ValueError):
        return handler(error, ctx)
    try:
        sig.bind(error, ctx)
    except (TypeError, ValueError):
        pass
    else:
        return handler(error, ctx)
    for args in ((error,), (ctx,), ()):
        try:
            sig.bind(*args)
        except (TypeError, ValueError):
            continue
        return handler(*args)
    return handler(error, ctx)


# Private backwards-compatible aliases.
_normalize_error_key = normalize_error_key
_normalize_action_key = normalize_action_key
_coerce_schema = coerce_schema
_action_candidates = action_candidates
_resolve_action_value = resolve_action_value
