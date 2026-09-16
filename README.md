# flask-utility

Reusable Flask utilities extracted from the BuyAutoParts codebase:

- **`ApiBuilder`** — convention-based CRUD endpoints for a SQLAlchemy model
  (collection + item routes, filtering, search, sort, pagination, hooks,
  decorators, nested `view_args`, singletons).
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
