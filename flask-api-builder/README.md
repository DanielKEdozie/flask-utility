# flask-api-builder

Convention-based RESTful CRUD API endpoints for Flask + SQLAlchemy + Marshmallow:

- **Automatic CRUD endpoints** for collection + item routes (`list`, `create`, `retrieve`, `update`, `patch`, `delete`).
- **Filtering, search, sorting, and pagination** out-of-the-box.
- **Nested routes & Singletons** (`view_args`, `view_args_ref`, `singleton=True`).
- **4-tier customization**:
  - `overrides={action: handler}` — replace action logic.
  - `schemas={action: schema}` — per-action read/write shapes.
  - `responses={action: handler}` — shape success responses globally & locally (first-class `'paginated'` and `'collection'` support).
  - `errors={code|alias: handler}` — shape errors globally & locally (first-class `422` validation support, status code inferred automatically).
- **Hooks & Decorators**: `before_create`, `after_create`, `before_update`, `after_update`, `before_delete`, `after_delete`, and method/resource decorators.

## Install

```bash
pip install flask-api-builder
```

## Quick Start

```python
from flask import Flask, Blueprint
from flask_sqlalchemy import SQLAlchemy
from flask_marshmallow import Marshmallow
from flask_api_builder import FlaskApiBuilder, ApiBuilder

app = Flask(__name__)
db = SQLAlchemy(app)
ma = Marshmallow(app)

# Initialize global error & response envelopes (no ma needed!):
api_ext = FlaskApiBuilder()
api_ext.init_app(app, db=db, responses={
    'paginated': lambda data, ctx: {
        'data': data['items'],
        'pagination': {
            'page': data['page'],
            'per_page': data['per_page'],
            'pages': data['pages'],
            'total': data['total'],
        }
    }
}, errors={
    422: lambda err, ctx: {'success': False, 'errors': err.messages},
    404: lambda err, ctx: {'success': False, 'message': 'Not found'},
})

api_bp = Blueprint('api', __name__, url_prefix='/api')

ApiBuilder(
    api_bp,
    model=Product,
    schema=ProductSchema,
    endpoint='products',
    url_prefix='/products',
    filter_fields=('category_id', 'is_active'),
    search_fields=('name', 'sku'),
    sort_field='name',
    paginate=True,
    # Custom RPC/action endpoints:
    extra_actions={
        'duplicate': lambda item, builder: {'duplicated_id': item.id},
        'stats': {
            'methods': ['GET'],
            'detail': False,
            'handler': lambda query, builder: {'total': query.count()},
        }
    }
)

app.register_blueprint(api_bp)
```

## Appending Custom Views & Methods (`extra_methods` / `extra_actions` / `@builder.action`)

Append custom logic and views directly onto an established route prefix (e.g. `/products`). Actions registered on a blueprint (e.g. `api`) automatically yield endpoint names like `api.products_publish` (accessible via `url_for('api.products_publish', publish_id=...)`).

You can write standard Flask-style views, inspect `request.method`, receive route variables by name, and choose whether to inherit from `ApiBuilder` or define your own schema:

```python
from flask import Blueprint, request
from flask_api_builder import ApiBuilder

api = Blueprint('api', __name__, url_prefix='/api')

# Define custom view with standard Flask logic:
def publish(publish_id=None):
    if request.method == 'POST':
        return {'status': 'published', 'id': publish_id}
    return {'status': 'draft', 'id': publish_id}

# Option A: Register via extra_methods in constructor
products = ApiBuilder(
    api,
    Product,
    endpoint='products',
    extra_methods={
        'publish': {
            'methods': ['POST', 'GET'],
            'path': '/<publish_id>/publish',
            'handler': publish,
        },
        'discount': {
            'methods': ['POST'],
            'path': '/<publish_id>/discount',
            'schema': DiscountSchema,  # Custom action schema
            'handler': lambda publish_id, data: {'id': publish_id, 'percent': data['percent']},
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
```

## License

MIT
