# flask-utility

Reusable Flask utilities extracted from the BuyAutoParts codebase:

- **`ApiBuilder`** — convention-based CRUD endpoints for a SQLAlchemy model
  (collection + item routes, filtering, search, sort, pagination, hooks,
  decorators, nested `view_args`, singletons, per-action `overrides`,
  per-action `schemas`/`responses`, per-builder + global `errors`).
- **`SchemaBuilder`** — builds a `marshmallow-sqlalchemy` schema class from
  a model with relationships, custom fields, method fields, and validators.
- **`flask_utility.events`** — SQLAlchemy event system. Models declare
  `slug_source` and `_events = ['insert', 'update']` to get automatic
  unique-slug generation; custom handlers can subscribe to the same hooks.
- **`flask_utility.models`** — framework-agnostic `SlugMixin`,
  `TimestampMixin`, and `BaseModel` helpers.

## Install

```bash
pip install -e ./flask-utility        # local development
# or, once published:
pip install flask-utility
```

## Quick start

```python
from flask import Flask, Blueprint
from flask_sqlalchemy import SQLAlchemy
from flask_marshmallow import Marshmallow
from flask_utility import ApiBuilder, SchemaBuilder, SlugMixin, init_model_events

db = SQLAlchemy()
ma = Marshmallow()

class Category(db.Model, SlugMixin):
    __tablename__ = 'categories'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)

    slug_source = 'name'          # field the slug is generated from
    _events = ['insert', 'update']  # regenerate on insert + update

app = Flask(__name__)
db.init_app(app)
ma.init_app(app)
init_model_events()  # wires the before_flush slug hook (once per process)

category_schema = SchemaBuilder(Category, ma=ma, sqla_session=db.session).schema

api_bp = Blueprint('api', __name__, url_prefix='/api')
ApiBuilder(api_bp, Category, category_schema, endpoint='categories',
           url_prefix='/categories', db_session=db.session)
```

## Model events

Any model can opt into automatic slug generation:

```python
class Part(db.Model, SlugMixin):
    slug_source = 'name'
    _events = ['insert']  # slug once on create; never rewrite the URL later
```

- Slugs are unique (`name`, `name-2`, `name-3`, …) per model table.
- On `update`, the slug only regenerates when the source field changed.
- Custom logic can hook the same pipeline:

```python
from flask_utility import on_model_event

@on_model_event('before_insert', MyModel)
def stamp_code(session, instance):
    instance.code = instance.code or uuid4().hex[:8]
```

## ApiBuilder actions: overrides, schemas, responses, errors

All four are **additive and opt-in**. When omitted, every default is
unchanged: plain schema dumps, `201` create / `200` read-update /
paginated `list`, `('', 204)` deletes, `400 Validation Error` /
`404 Resource not found` / `{message, status}` errors.

Canonical actions are `list` (`GET` collection), `create` (`POST`
collection), `retrieve` (`GET` item/singleton), `update` (`PUT` item, and
`PATCH`/`POST`-upsert unless split), `patch` (optional `PATCH`-only
override; falls back to `update`), `delete` (`DELETE`). Legacy keys
(`collection`, `item`, `singleton`, `GET`/`POST`/`PUT`/`PATCH`/`DELETE`,
`POST_collection`, endpoint name) still resolve most-specific-first, and
subclass attributes work like `hooks` (`overrides`, `schemas`,
`responses`, `errors`; aliases `action_override`, `response_schemas`,
`response_handlers`, `error_handlers`).

### 1. `overrides={action: handler}` — replace handler logic

The builder owns `jsonify`/status/headers. Return plain data (Flask view
conventions): `data`, `(data, status)`, `(data, headers)`, `(data, status,
headers)`, or a Flask `Response` (passes through, backward-compatible):

```python
def audit_create(data, builder):
    # `data` = parsed JSON with view_args injected; `builder` exposes
    # .model/.schema/.session helpers.
    item = builder.schema.load(data)
    builder.session.add(item)
    builder.session.commit()
    return builder.schema.dump(item), 201

def cached_list(query, builder):
    return builder.schema.dump(query.all(), many=True)

def with_headers(query, builder):
    return {'items': [...]}, 200, {'X-Total': '3'}

ApiBuilder(bp, Part, PartSchema, endpoint='parts',
           overrides={'create': audit_create, 'list': cached_list})
```

Handlers declare any subset by name: `query` (list), `item` (`None` for
collection-create / missing singleton), `data` (writes), `route_values`,
`request`, `builder`/`api_builder`, `action`, `schema`, `session`.
Leading-positional form (`handler(item, data, route_values, request,
builder)`, same convention as `hooks`) also works. Overrides run inside
`view_args` validation, per-method `decorators`, and the `errors`
pipeline (`ValidationError`/generic → `errors` + rollback;
`HTTPException` propagates to the error wrapper).

### 2. `schemas={action: schema}` — per-action read/write shapes

```python
ApiBuilder(bp, Part, PartSchema, endpoint='parts', schemas={
    'list': PartListSchema,       # lean `only=('id','name')` shape
    'retrieve': PartDetailSchema, # nested relationships
    'create': PartCreateSchema,   # write schema used for load + dump
})
```

Values accept a schema instance, schema class, or `SchemaBuilder`
(`.schema` is coerced automatically). Unmapped actions use the default
`schema`. Singleton upserts resolve dynamically: missing row → `create`
schema, present row → `update`/`patch` schema.

### 3. `responses={action: handler}` — shape success payloads globally + locally

Success-only. Handlers receive dumped `data` and `ctx = {action, status,
request, route_values, builder}`. Short forms `handler(data, ctx)` and
`handler(data)` work. Return plain `data` (status is preserved) or
`(data, status)`.

**Global vs. Local**: Define `responses` once on `FlaskUtility` to give every
endpoint in your app a unified response envelope. Per-builder `responses`
extend and override the globals:

```python
from flask_utility import FlaskUtility, ApiBuilder

# Global responses for the entire application:
utility = FlaskUtility()
utility.init_app(app, db=db, ma=ma, responses={
    # 1. Specialized for paginated list endpoints:
    'paginated': lambda data, ctx: {
        'data': data['items'],
        'pagination': {
            'page': data['page'],
            'per_page': data['per_page'],
            'pages': data['pages'],
            'total': data['total'],
        }
    },
    # 2. Specialized for unpaginated collections (paginate=False):
    'collection': lambda data, ctx: {
        'data': data,
        'count': len(data),
    },
    # 3. Single-item retrieve:
    'retrieve': lambda data, ctx: {'data': data},
    # 4. Resource creation:
    'create': lambda data, ctx: ({'success': True, 'data': data}, 201),
})

# Any ApiBuilder can extend or override specific actions locally:
ApiBuilder(bp, Part, PartSchema, endpoint='parts', responses={
    'paginated': lambda data, ctx: {'parts': data['items'], 'total': data['total']},
})
```

Canonical keys:
- `'paginated'` — used specifically when `paginate=True` collection lists return `{items, page, per_page, pages, total}`.
- `'collection'` — used for unpaginated collection lists (`paginate=False`).
- `'list'` — fallback covering both paginated and unpaginated collection lists.
- `'retrieve'` — single-item GET.
- `'create'` — POST collection / singleton create.
- `'update'` / `'patch'` — PUT/PATCH item updates.
- `'delete'` — DELETE (defaults to `('', 204)` unless `'delete'` is explicitly defined).

### 4. `errors={code|alias: handler}` — shape errors globally + locally

```python
from flask_utility import FlaskUtility

utility = FlaskUtility()
utility.init_app(app, db=db, ma=ma, errors={
    # Status code is automatically 404 (inferred from ctx['status']), no tuple needed:
    404: lambda error, ctx: {'success': False, 'message': 'Not found'},
})

ApiBuilder(bp, Part, PartSchema, endpoint='parts', errors={
    # 422 is supported as a first-class validation key (automatically emits HTTP 422):
    422: lambda e, ctx: {'message': 'Invalid input', 'errors': e.messages},
    # Or keep 400 for legacy validation:
    'validation': lambda e, ctx: {'message': 'Invalid', 'errors': e.messages},
})
```

Keys are status codes (`400`, `404`, `422`, `500`, …) or aliases
(`'validation'`, `'unprocessable_entity'`, `'not_found'`, `'generic'`,
`'http'`).

- **Inferring status codes**: Handlers can return a plain body (e.g. `dict`
  or `list`), and `ctx['status']` is used automatically. You only need to
  return `(body, status)` if you want to override the HTTP status code.
- **`422` vs `400` validation**: For schema validation errors, `422` is
  supported as a first-class key alongside `'validation'` and `400`. When
  matched via `422` (or alias `'unprocessable_entity'`), the response status
  code automatically defaults to `422`.
- **Resolution**: specific code first, then alias (`422`/`400` →
  `'validation'`/`'generic'`, `404` → `'not_found'`, any `HTTPException` →
  `'http'`).
- **Signature flexibility**: `handler(error, ctx)` with `ctx = {action,
  status, request, route_values, builder, error_kind}` returns a body or
  `(body, status)`; short forms `handler(error)` / `handler(ctx)` /
  `handler()` work.
- Per-builder `errors` merge over `FlaskUtility(errors=...)` globals (builder
  wins per key).
- **Defaults preserved when unmapped**: `400 {'message': 'Validation Error',
  'errors'}` for `ValidationError` (unless matched by `422` or `'validation'`),
  `404 {'message': 'Resource not found'}`, `{message, status}` for
  `HTTPException`, `400 {'message': str(error)}` otherwise.

### 5. Appending Custom Views & Methods (`extra_methods` / `extra_actions` / `@builder.action`)

Append custom logic and views directly onto an established route prefix (e.g. `/products`). Actions registered on a blueprint (e.g. `api`) automatically yield endpoint names like `api.products_publish` (accessible via `url_for('api.products_publish', publish_id=...)`).

You can write standard Flask-style views, inspect `request.method`, receive route variables by name, and choose whether to inherit from `ApiBuilder` or define your own schema:

```python
from flask import Blueprint, request
from flask_utility import ApiBuilder

api = Blueprint('api', __name__, url_prefix='/api')

# Define custom logic with standard view signatures:
def publish(publish_id=None):
    if request.method == 'POST':
        # Custom publish logic
        return {'status': 'published', 'id': publish_id}
    return {'status': 'draft', 'id': publish_id}

# Option A: Register via extra_methods (or extra_actions) in constructor
products = ApiBuilder(
    api,
    Product,
    endpoint='products',  # url_prefix defaults to '/products'
    extra_methods={
        'publish': {
            'methods': ['POST', 'GET'],
            'path': '/<publish_id>/publish',
            'handler': publish,
        },
        'discount': {
            'methods': ['POST'],
            'path': '/<publish_id>/discount',
            'schema': DiscountSchema,  # Action has its own validation/dump schema
            'handler': lambda publish_id, data: {'id': publish_id, 'percent': data['percent']},
        },
        'raw_stream': {
            'methods': ['GET'],
            'path': '/stream',
            'inherit': False,  # Opt out of ApiBuilder error/response wrappers
            'handler': lambda: ('stream-data', 200, {'Content-Type': 'text/plain'}),
        },
    }
)

# Option B: Register dynamically with decorators
@products.action('archive', path='/<publish_id>/archive', methods=['POST'])
def archive_product(builder, publish_id=None):
    # Access builder.session and builder.model directly:
    item = builder.session.get(builder.model, int(publish_id))
    item.status = 'archived'
    builder.session.commit()
    return {'status': item.status, 'id': publish_id}

@products.route('/<publish_id>/toggle', methods=['POST'])
def toggle_status(item):
    # Declaring 'item' automatically loads the database model (returns 404 if missing)
    item.active = not item.active
    return item
```

#### Key Capabilities:
- **Established Route Mounting**: All paths append to the resource prefix (e.g. `/api/products/<publish_id>/publish`).
- **Standard Flask Endpoints**: On blueprint `api` with endpoint `products`, actions register as `api.products_publish`, `api.products_archive`, etc., fully compatible with `url_for()`.
- **Intelligent Signature Binding**: Handlers can declare route parameters directly (e.g. `publish_id=None`, `id=None`), context injectables (`builder`, `session`, `model`, `schema`, `data`), or `item` for automatic model lookup.
- **Inherit vs Custom Schema**:
  - `inherit=True` (default): Uses `ApiBuilder`'s error pipeline (emitting 422 for schema validation, 404 for not found) and response envelopes.
  - `schema=MySchema`: Action validates request payloads and serializes responses using its own dedicated schema.
  - `inherit=False`: Runs as a raw Flask view without builder wrappers.

## v2: MethodView + `method_endpoints`

v2 registers per-action `MethodView`s (`CollectionView` → `do_list`/`do_create`, `ItemView` → `do_retrieve`/`do_update`/`do_delete`, `SingletonView` → upsert units). Logic is unchanged; only dispatch + endpoint names are cleaner. `from flask_utility import CollectionView, ItemView, SingletonView` for subclass chains.

Each action gets its own endpoint via `method_endpoints` (alias `endpoint_names`), resolved with the same chain as `overrides`. Defaults are canonical:

```python
ApiBuilder(bp, Product, ProductSchema, endpoint='products')
# → api.products_list (GET /products)
# → api.products_create (POST /products)
# → api.products_retrieve / _update / _patch / _delete (GET/PUT/PATCH/DELETE /products/<id>)
```

Customize the suffix appender:

```python
ApiBuilder(..., method_endpoints={'retrieve': 'detail'})
# → api.products_detail instead of api.products_retrieve

ApiBuilder(..., method_endpoints={'collection': 'all'})
# list + create share one endpoint+rule: api.things_all (merged methods, no clash)
```

Migration v1 → v2: `products_collection` → `products_list` + `products_create`; `products_detail` → `products_retrieve` (+`_update/_patch/_delete`); `products_singleton` → per-action singleton endpoints. Same URLs, new `url_for` names. Validation default is `422` (`400` only with a `400`-keyed handler).



