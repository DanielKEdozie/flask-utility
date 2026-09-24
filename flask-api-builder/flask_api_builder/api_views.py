"""Flask ``MethodView`` adapters for :class:`ApiBuilder`.

Thin dispatch layer — all logic lives in the builder's ``do_*`` units
(``do_list/do_create/do_retrieve/do_update/do_delete`` + singleton
variants) so function-view behavior and ``MethodView`` behavior are
identical. ``overrides/schemas/responses/errors``, ``hooks``,
``query_override``, ``view_args`` and extra actions are untouched.
"""
from flask.views import MethodView


class CollectionView(MethodView):
    """``GET`` → ``do_list``, ``POST`` → ``do_create``."""

    def __init__(self, builder):
        self.builder = builder

    def get(self, **route_values):
        return self.builder.do_list(dict(route_values))

    def post(self, **route_values):
        return self.builder.do_create(dict(route_values))


class ItemView(MethodView):
    """``GET/PUT/PATCH/DELETE`` → retrieve / update / delete."""

    def __init__(self, builder):
        self.builder = builder

    def get(self, **route_values):
        return self.builder.do_retrieve(dict(route_values))

    def put(self, **route_values):
        return self.builder.do_update(dict(route_values), partial=False)

    def patch(self, **route_values):
        return self.builder.do_update(dict(route_values), partial=True)

    def delete(self, **route_values):
        return self.builder.do_delete(dict(route_values))


class SingletonView(MethodView):
    """Single-resource endpoint (upsert semantics preserved)."""

    def __init__(self, builder):
        self.builder = builder

    def get(self, **route_values):
        return self.builder.do_singleton_retrieve(dict(route_values))

    def post(self, **route_values):
        return self.builder.do_singleton_write(dict(route_values), method='POST')

    def put(self, **route_values):
        return self.builder.do_singleton_write(dict(route_values), method='PUT')

    def patch(self, **route_values):
        return self.builder.do_singleton_write(dict(route_values), method='PATCH')

    def delete(self, **route_values):
        return self.builder.do_singleton_delete(dict(route_values))
