from inspect import signature

from marshmallow import ValidationError, fields, validates_schema
from sqlalchemy import inspect as sa_inspect

from .extension import resolve_ma, resolve_session


#: Top-level keys of the new-style ``relationships`` auto-configuration
#: mapping: ``{'only': ..., 'exclude': ..., 'rel_fields': {...}}``.
_AUTO_RELATIONSHIP_KEYS = frozenset(('only', 'exclude', 'rel_fields'))

#: Per-field options accepted inside ``rel_fields`` entries besides
#: ``only``/``exclude``. These are applied to the generated nested field
#: (``write=False`` means response-only, i.e. Marshmallow ``dump_only``).
_REL_FIELD_FLAGS = frozenset((
    'write', 'read_only', 'dump_only', 'load_only', 'required', 'allow_none',
))

#: All keys accepted inside ``rel_fields`` entries. ``depth`` renders that
#: field's subtree as if built with ``SchemaBuilder(..., depth=<value>)``,
#: i.e. at most ``depth`` levels of that field (``0`` disables nesting of
#: the field entirely, leaving a flat leaf).
_REL_FIELD_KEYS = (
    _AUTO_RELATIONSHIP_KEYS | _REL_FIELD_FLAGS | frozenset(('depth',))
)


def _is_auto_relationship_config(value):
    """Return True when ``relationships`` uses the auto-config shape.

    New style::

        relationships={
            'only': ('parent', 'children'),
            'exclude': ('products',),
            'rel_fields': {
                'parent': {'only': ('id', 'name'), 'write': False},
            },
        }

    Anything else (field names mapped to schemas, builders, or explicit
    configuration dicts) is treated as legacy explicit nested definitions.
    """
    if not isinstance(value, dict) or not value:
        return False
    if set(value) - _AUTO_RELATIONSHIP_KEYS:
        return False
    for key, item in value.items():
        if key in ('only', 'exclude'):
            if item is None:
                continue
            if isinstance(item, dict) or isinstance(item, str):
                return False
            try:
                fields = tuple(item)
            except TypeError:
                return False
            if any(not isinstance(name, str) for name in fields):
                return False
        elif key == 'rel_fields':
            if not isinstance(item, dict):
                return False
            for field_config in item.values():
                if not isinstance(field_config, dict):
                    return False
                if set(field_config) - _REL_FIELD_KEYS:
                    return False
                field_depth = field_config.get('depth')
                if (
                    field_depth is not None
                    and (isinstance(field_depth, bool) or not isinstance(field_depth, int))
                ):
                    return False
    return True


class SchemaBuilder:
    """Build a ``marshmallow-sqlalchemy`` schema for a SQLAlchemy model.

    A ``SchemaBuilder`` creates a schema class immediately and stores it in
    :attr:`schema`. The generated schema subclasses
    ``ma.SQLAlchemyAutoSchema`` and uses the model's SQLAlchemy columns to
    generate fields. It is configured with ``load_instance=True`` and the
    application's ``db.session``, so ``load`` returns model instances that
    can be committed by :class:`ApiBuilder`.

    Basic usage::

        from marshmallow import fields

        part_schema = SchemaBuilder(
            Part,
            exclude=('created_at', 'internal_note'),
            custom_fields={
                'name': fields.String(required=True),
            },
            methods={
                'display_name': lambda part: '{} ({})'.format(
                    part.name, part.sku
                ),
            },
        )

        # Pass the generated schema class to ApiBuilder.
        PartApi(api_bp, Part, part_schema.schema, endpoint='parts')

    The builder itself is callable as a convenience::

        schema_instance = part_schema()
        item = schema_instance.load({'name': 'Brake Pad', 'sku': 'BP-1'})
        output = schema_instance.dump(item)

    Constructor options:

    ``model``
        SQLAlchemy model class used for automatic field generation. The
        model must be compatible with ``marshmallow-sqlalchemy``.

    ``exclude`` and ``only``
        Iterable of field names to exclude or include. These values are
        passed to the generated schema's ``Meta`` class. Use ``only`` for a
        public response shape and ``exclude`` for sensitive/internal fields.

    ``dump_only`` and ``load_only``
        Iterable of generated field names that are response-only or
        request-only. ``dump_fields`` is accepted as an alias for
        ``dump_only``. These options also work for custom fields through the
        normal Marshmallow field constructor options.

    ``depth``
        Maximum automatic relationship depth. It is used when
        ``auto_relationships=True``; explicit relationship definitions are
        not limited by this value.

    ``auto_relationships``
        When true, discover SQLAlchemy relationships from the model and build
        nested schemas automatically. Explicit entries in ``nested_schema``
        take precedence. It is disabled by default so response shapes remain
        intentional. Per-relationship tuning lives in ``relationships``.

    ``relationships``
        Auto-relationship configuration mapping with ``only`` and
        ``exclude`` keys selecting which relationship attributes are
        declared. Per-field configuration lives in the top-level
        ``rel_fields`` argument::

            SchemaBuilder(
                Category,
                auto_relationships=True,
                depth=4,
                relationships={'exclude': ('products',)},
                rel_fields={
                    'parent': {
                        'only': ('id', 'name', 'slug'),
                        'write': False,
                    },
                    'children': {
                        'exclude': ('parent',),
                        'depth': 3,
                    },
                },
            )

        For backwards compatibility, a ``rel_fields`` key inside this
        mapping is still accepted (top-level ``rel_fields`` wins on
        conflict), as is a mapping of relationship field names to
        schemas/builders/config dicts, which behaves like
        ``nested_schema``. The ``relationship_only``,
        ``relationship_exclude``, ``include_relationships``, and
        ``exclude_relationships`` keyword arguments (plus their aliases) are
        also still accepted; values from ``relationships`` take precedence.

    ``rel_fields``
        Per-field relationship configuration, mapping a field name to
        ``{'only': ..., 'exclude': ..., 'write': ..., 'depth': ...}``
        options. ``only``/``exclude`` control that field's nested schema,
        ``write`` its writability (``write=False`` means response-only),
        and ``depth`` renders that field's subtree as if built with that
        global ``depth`` (``0`` leaves a flat leaf). Applies at every level
        of the auto-built tree.

    ``nested_schema``
        Explicit nested/custom schema definitions, mapping a relationship
        field name to a schema class, a ``SchemaBuilder``, a schema
        instance, or a configuration dictionary. A configuration dictionary
        may provide ``schema`` or ``model``. When ``model`` is provided, a
        private nested ``SchemaBuilder`` is created and does not register
        anything globally. Set ``many=True`` for collection relationships.
        Use ``write=False`` or ``read_only=True`` to make a nested attribute
        response-only. The equivalent Marshmallow option is
        ``dump_only=True``. Use ``load_only=True`` for request-only nested
        data. Dictionaries also accept ``only`` and ``exclude`` to control
        the nested schema independently of the parent schema::

            user_schema = SchemaBuilder(User)
            part_schema = SchemaBuilder(
                Part,
                nested_schema={
                    'owner': user_schema,
                    'images': {
                        'model': Image,
                        'schema_name': 'PartImageSchema',
                        'many': True,
                        'only': ('id', 'url'),
                        'write': False,
                    },
                    'secret': {
                        'schema': SecretSchema,
                        'exclude': ('internal_value',),
                        'dump_only': True,
                    },
                },
            )

        A string in ``schema`` is treated as ``schema_name`` when ``model``
        is also provided. A string without ``model`` cannot be resolved
        automatically and raises ``ValueError``; the builder intentionally
        does not maintain a global schema registry.

    ``custom_fields``
        Mapping of field name to a Marshmallow field instance. Field options
        such as ``dump_only``, ``load_only``, ``required``, and ``allow_none``
        should normally be passed directly to the Marshmallow field::

            custom_fields={
                'email': fields.Email(required=True),
                'is_available': fields.Boolean(dump_only=True),
            }

        A dictionary form is also supported when those options need to be
        applied after field creation::

            custom_fields={
                'internal_code': {
                    'field': fields.String(),
                    'dump_only': True,
                },
            }

    ``methods``
        Mapping of output field name to a callable. Callables are exposed as
        ``fields.Method`` fields and are intended for simple computed output::

            methods={'full_name': lambda user: '{} {}'.format(
                user.first_name, user.last_name
            )}

        Use ``custom_fields`` for prebuilt fields such as
        ``fields.Function`` when custom serialize/deserialize behavior is
        needed.

    ``custom_validators``
        Mapping of field name to a validator callable. A validator receives
        ``value`` or ``value, data`` and runs during schema-level validation.
        Return ``False`` or raise ``ValidationError`` to reject the input::

            custom_validators={
                'sku': lambda value: value.startswith('SKU-'),
            }

    ``schema_validator``
        Callable receiving ``data`` and Marshmallow validation keyword
        arguments. Use it for rules involving multiple fields; raise
        ``ValidationError`` with field messages when invalid::

            def validate_dates(data, **kwargs):
                if data.get('end') < data.get('start'):
                    raise ValidationError({'end': ['Must follow start.']})

    ``schema_name``
        Name of the generated schema class. The default is
        ``<ModelName>Schema``.

    Additional keyword arguments are used as generated ``Meta`` options. To
    provide custom ``Meta`` attributes, pass a mapping under ``Meta``::

        SchemaBuilder(Part, Meta={'ordered': True})

    The generated schema class is available through ``.schema`` and can be
    passed to ``ApiBuilder``. The builder does not initialize ``db`` or ``ma``;
    those extensions must be initialized by the Flask application first.

    ``build_form`` can optionally generate a WTForms-Alchemy model form from
    the same SQLAlchemy model::

        PartForm = part_schema.build_form(
            include_foreign_keys=True,
            all_fields_optional=True,
        )
        form = PartForm(obj=part)

    WTForms-Alchemy must be installed separately. Marshmallow ``custom_fields``
    and ``methods`` are not copied into WTForms forms; use WTForms-Alchemy's
    ``field_args``, ``include``, ``exclude``, or custom form fields for form
    specific behavior.
    """

    def __init__(
        self,
        model,
        exclude=None,
        only=None,
        dump_only=None,
        load_only=None,
        dump_fields=None,
        depth=1, # depth of relationships,
        auto_relationships=False,
        relationship_exclude=None,
        exclude_from_relationship=None,
        relationship_only=None,
        include_relationships=None,
        exclude_relationships=None,
        include_rel_only=None,
        rel_exclude=None,
        relationships=None, # {only, exclude} (+ legacy rel_fields)
        rel_fields=None, # {field: {only, exclude, write, depth}}
        nested_schema=None, # {name: SchemaBuilder, schema class/instance, or config dict}
        custom_fields=None, # create custom fields or override fields,
        methods=None, # {name, function},
        custom_validators=None, # {name, function},
        schema_validator=None, # callable function recieves validates_schema args,
        schema_name=None, # name of schema,
        ma=None, # Flask-Marshmallow instance (else via FlaskUtility extension)
        sqla_session=None, # SQLAlchemy session (else via FlaskUtility extension)
        **kwargs
    ):
        """Create and configure a generated SQLAlchemy schema class.

        Args:
            model: SQLAlchemy model class to serialize and deserialize.
            exclude: Field names omitted from the generated schema.
            only: Field names included in the generated schema.
            dump_only: Response-only generated field names.
            load_only: Request-only generated field names.
            dump_fields: Alias for dump_only.
            depth: Maximum automatic relationship depth.
            auto_relationships: Automatically build nested relationship schemas.
            relationship_exclude: Fields to exclude from nested relationships.
            exclude_from_relationship: Alias for relationship_exclude.
            relationship_only: Fields to include in each nested relationship.
            include_relationships: Relationship attributes to include.
            exclude_relationships: Relationship attributes to exclude.
            include_rel_only: Alias for include_relationships.
            rel_exclude: Alias for exclude_relationships.
            relationships: Auto-relationship configuration
                ``{'only': ..., 'exclude': ...}``. ``only``/``exclude``
                select which relationship attributes are declared.
                Per-field configuration lives in ``rel_fields``; a
                ``rel_fields`` key here is still accepted for backwards
                compatibility (top-level ``rel_fields`` wins on conflict).
                A legacy mapping of field names to schemas/builders/config
                dicts is still accepted and behaves like ``nested_schema``.
            rel_fields: Per-field relationship configuration mapping a
                field name to
                ``{'only': ..., 'exclude': ..., 'write': ..., 'depth': ...}``
                options. ``only``/``exclude`` control that field's nested
                schema, ``write`` its writability (``write=False`` means
                response-only), and ``depth`` renders that field's subtree
                as if built with that global ``depth`` (``0`` leaves a
                flat leaf). Applies at every level of the auto-built tree.
            nested_schema: Explicit nested/custom schema definitions mapping
                a relationship field name to a schema class, a
                ``SchemaBuilder``, a schema instance, or a configuration
                dictionary (same forms as the legacy ``relationships``).
                Explicit entries take precedence over auto-built ones.
            custom_fields: Marshmallow fields to add or replace.
            methods: Computed output fields mapped to callables.
            custom_validators: Field validators mapped by field name.
            schema_validator: Cross-field validation callable.
            schema_name: Name assigned to the generated schema class.
            **kwargs: Additional generated ``Meta`` options.
        """
        self.model = model
        self.exclude = tuple(exclude or ())
        self.only = tuple(only or ())
        self.dump_only = tuple(dump_only if dump_only is not None else dump_fields or ())
        self.load_only = tuple(load_only or ())
        self.depth = depth
        self.auto_relationships = auto_relationships
        raw_relationships = dict(relationships or {})
        self.nested_schema = dict(nested_schema or {})
        auto_config = {}
        if raw_relationships:
            if _is_auto_relationship_config(raw_relationships):
                auto_config = raw_relationships
            else:
                # Legacy shape: explicit per-field schema definitions.
                # Fold them into nested_schema (explicit nested_schema wins).
                self.nested_schema = {**raw_relationships, **self.nested_schema}
        # Deprecated alias kept for introspection; nested_schema is canonical.
        self.relationships = dict(self.nested_schema)
        # Per-field config: top-level rel_fields wins over the nested
        # relationships={'rel_fields': ...} form (both still accepted).
        self.rel_fields = {
            **dict(auto_config.get('rel_fields') or {}),
            **dict(rel_fields or {}),
        }
        for field, config in self.rel_fields.items():
            if not isinstance(config, dict):
                raise ValueError(
                    "rel_fields {!r} must map to a configuration dict".format(field)
                )
            if set(config) - _REL_FIELD_KEYS:
                raise ValueError(
                    "rel_fields {!r} has unknown keys: {}".format(
                        field, sorted(set(config) - _REL_FIELD_KEYS)
                    )
                )
            field_depth = config.get('depth')
            if field_depth is not None and (
                isinstance(field_depth, bool)
                or not isinstance(field_depth, int)
                or field_depth < 0
            ):
                raise ValueError(
                    "rel_fields {!r} depth must be a non-negative integer".format(field)
                )
        self.rel_field_options = {
            field: {
                key: value
                for key, value in config.items()
                if key in _REL_FIELD_FLAGS
            }
            for field, config in self.rel_fields.items()
        }
        self.rel_field_options = {
            field: options
            for field, options in self.rel_field_options.items()
            if options
        }
        self.relationship_exclude = dict(
            relationship_exclude
            if relationship_exclude is not None
            else exclude_from_relationship or {}
        )
        self.relationship_only = dict(relationship_only or {})
        for field, config in self.rel_fields.items():
            if config.get('only') is not None:
                self.relationship_only[field] = config['only']
            if config.get('exclude') is not None:
                self.relationship_exclude[field] = config['exclude']
        if 'only' in auto_config:
            configured_includes = auto_config['only']
        else:
            configured_includes = (
                include_relationships
                if include_relationships is not None
                else include_rel_only
            )
        if 'exclude' in auto_config:
            configured_excludes = auto_config['exclude']
        else:
            configured_excludes = (
                exclude_relationships
                if exclude_relationships is not None
                else rel_exclude
            )
        self.include_relationships = (
            set(configured_includes) if configured_includes is not None else None
        )
        self.exclude_relationships = set(configured_excludes or ())
        self.custom_fields = dict(custom_fields or {})
        self.methods = dict(methods or {})
        self.custom_validators = dict(custom_validators or {})
        self.schema_validator = schema_validator
        self.schema_name = schema_name or '{}Schema'.format(model.__name__)
        self.options = dict(kwargs)
        # Explicit instances win; otherwise resolve through the
        # FlaskUtility extension bound to the current app.
        self._ma = resolve_ma(ma)
        self._sqla_session = resolve_session(sqla_session)
        self.schema = self.build()

    def __call__(self, *args, **kwargs):
        """Create a schema instance from the generated schema class."""
        return self.schema(*args, **kwargs)

    def build_form(
        self,
        form_name=None,
        form_fields=None,
        override_fields=None,
        label_names=None,
        form_widgets=None,
        override_widgets=None,
        **options
    ):
        """Build and return a WTForms-Alchemy form class for this model.

        Existing ``only`` and ``exclude`` settings are used unless explicitly
        overridden in ``options``. ``dump_only`` fields are excluded because
        they are response-only Marshmallow fields. ``form_fields`` maps field
        names to unbound WTForms field instances and overrides generated form
        fields with the same name. ``label_names`` maps field names to display
        labels. ``form_widgets`` maps field names to WTForms widget instances.
        ``override_fields`` and ``override_widgets`` are aliases for the
        corresponding ``form_*`` arguments.

        Example::

            from wtforms import TextAreaField
            from wtforms.validators import Length

            PartForm = part_schema.build_form(
                form_fields={
                    'description': TextAreaField(
                        validators=[Length(max=500)]
                    ),
                },
            )
        """
        from wtforms_alchemy import model_form_factory

        form_options = dict(options)
        if form_fields is None:
            form_fields = override_fields
        if form_widgets is None:
            form_widgets = override_widgets

        label_names = dict(label_names or {})
        field_args = dict(form_options.get('field_args') or {})
        for field_name, label in label_names.items():
            field_args.setdefault(field_name, {})
            field_args[field_name] = dict(field_args[field_name])
            field_args[field_name].setdefault('label', label)
        if field_args:
            form_options['field_args'] = field_args

        # WTForms-Alchemy auto-skips primary keys and datetimes with
        # defaults before applying exclude; attr_errors=False keeps those
        # names from raising InvalidAttributeException.
        form_options.setdefault('attr_errors', False)
        form_options.setdefault('only', self.only or None)
        configured_exclude = set(form_options.get('exclude') or self.exclude)
        configured_exclude.update(self.dump_only)
        form_options['exclude'] = tuple(configured_exclude)

        form_base = model_form_factory(**form_options)
        # Inherit the base Meta so options consumed by the factory
        # (field_args, validators, etc.) are preserved on the form.
        form_meta = type(
            'Meta',
            (form_base.Meta,),
            {
                'model': self.model,
                'only': form_options.pop('only', None),
                'exclude': form_options.pop('exclude', ()),
            },
        )
        form_attributes = dict(form_fields or {})
        # Apply label_names to custom form fields too: WTForms-Alchemy only
        # applies field_args to fields it generates itself. UnboundField
        # passes args positionally at bind time, so the label must be the
        # first positional arg.
        for field_name, label in label_names.items():
            field = form_attributes.get(field_name)
            if field is not None and not field.args:
                field.args = (label,)
        form_attributes['Meta'] = form_meta
        form_widgets = dict(form_widgets or {})

        if form_widgets:
            def form_init(form, *args, **kwargs):
                form_base.__init__(form, *args, **kwargs)
                for field_name, widget in form_widgets.items():
                    if field_name not in form._fields:
                        continue
                    form._fields[field_name].widget = (
                        widget() if isinstance(widget, type) else widget
                    )

            form_attributes['__init__'] = form_init

        form_name = form_name or '{}Form'.format(self.model.__name__)
        return type(form_name, (form_base,), form_attributes)

    def build(self):
        """Return the generated ``SQLAlchemyAutoSchema`` class."""
        schema_attributes = self._custom_field_attributes()
        schema_attributes.update(self._method_fields())
        schema_attributes.update(self._relationship_fields())

        base_meta = {
            'model': self.model,
            'load_instance': True,
            'sqla_session': self._sqla_session,
            'include_fk': True,
            'include_relationships': False,
            'exclude': self.exclude,
        }
        if self.only:
            base_meta['fields'] = self.only
        base_meta.update(self.options.pop('Meta', {}))
        schema_attributes['Meta'] = type('Meta', (), base_meta)

        dump_only = set(self.dump_only)
        load_only = set(self.load_only)

        def on_bind_field(schema, field_name, field):
            if field_name in dump_only:
                field.dump_only = True
            if field_name in load_only:
                field.load_only = True

        schema_attributes['on_bind_field'] = on_bind_field

        validator = self._schema_validator()
        if validator is not None:
            schema_attributes['_validate_schema'] = validates_schema(validator)

        return type(self.schema_name, (self._ma.SQLAlchemyAutoSchema,), schema_attributes)

    def _custom_field_attributes(self):
        result = {}
        for name, configured_field in self.custom_fields.items():
            if not isinstance(configured_field, dict):
                result[name] = configured_field
                continue

            field = configured_field.get('field')
            if field is None:
                raise ValueError(
                    "Custom field '{}' must include a 'field' value".format(name)
                )
            result[name] = self._configure_field(field, configured_field)
        return result

    def _method_fields(self):
        result = {}
        for name, method in self.methods.items():
            method_name = '_method_{}'.format(name)
            def schema_method(schema, obj, *args, _method=method, **kwargs):
                return _method(obj)

            result[method_name] = schema_method
            result[name] = fields.Method(method_name)
        return result

    def _relationship_fields(self):
        relationship_configs = dict(self.nested_schema)
        if self.auto_relationships and self.depth > 0:
            inherited = {}
            if self.exclude_relationships:
                inherited['exclude_relationships'] = tuple(self.exclude_relationships)
            if self.include_relationships is not None:
                inherited['include_relationships'] = tuple(self.include_relationships)
            if self.relationship_exclude:
                inherited['relationship_exclude'] = dict(self.relationship_exclude)
            if self.relationship_only:
                inherited['relationship_only'] = dict(self.relationship_only)
            if self.rel_fields:
                # Re-parsed by each nested builder, so per-field config
                # applies at every level of the auto-built tree.
                inherited['relationships'] = {
                    'rel_fields': {
                        name: dict(config)
                        for name, config in self.rel_fields.items()
                    }
                }
            for name, relationship in sa_inspect(self.model).relationships.items():
                entry = dict({
                    'model': relationship.mapper.class_,
                    'many': relationship.uselist,
                    'schema_name': '{}{}Schema'.format(
                        self.model.__name__, name.title()
                    ),
                    'auto_relationships': True,
                    'depth': self.depth - 1,
                    'write': False,
                }, **inherited)
                if name in self.rel_field_options:
                    entry.update(self.rel_field_options[name])
                field_depth = self.rel_fields.get(name, {}).get('depth')
                if field_depth is not None:
                    # Per-field depth uses the same units as the global
                    # depth: the field's subtree renders as if built with
                    # SchemaBuilder(..., depth=field_depth). Consuming one
                    # level for this nesting keeps depths decreasing so the
                    # tree always terminates.
                    entry['depth'] = min(entry['depth'], field_depth - 1)
                relationship_configs.setdefault(name, entry)

        if self.include_relationships is not None:
            relationship_configs = {
                name: config
                for name, config in relationship_configs.items()
                if name in self.include_relationships
            }
        if self.only:
            relationship_configs = {
                name: config
                for name, config in relationship_configs.items()
                if name in self.only
            }
        relationship_configs = {
            name: config
            for name, config in relationship_configs.items()
            if name not in self.exclude_relationships
        }

        result = {}
        model_relationships = {
            relationship.key: relationship
            for relationship in sa_inspect(self.model).relationships
        }
        for name, relationship in relationship_configs.items():
            many = False
            schema = relationship
            options = {}
            if isinstance(relationship, dict):
                schema = relationship.get('schema')
                many = relationship.get('many', False)
                options = dict(relationship)
            if schema is None and options.get('model') is None:
                model_relationship = model_relationships.get(name)
                if model_relationship is not None:
                    options['model'] = model_relationship.mapper.class_
                    if 'many' not in options:
                        many = model_relationship.uselist
            if name in self.relationship_exclude and 'exclude' not in options:
                options['exclude'] = self.relationship_exclude[name]
            if name in self.relationship_only and 'only' not in options:
                options['only'] = self.relationship_only[name]
            if isinstance(schema, str) and options.get('model') is not None:
                options['schema_name'] = schema
                schema = None
            if schema is None and options.get('model') is not None:
                schema = self._build_nested_schema(options)
            if isinstance(schema, SchemaBuilder):
                schema = schema.schema
            if isinstance(schema, str):
                raise ValueError(
                    'String relationship schemas must be resolved before building {}'.format(
                        self.schema_name
                    )
                )
            nested_options = {
                'many': many,
                'allow_none': options.get('allow_none', True),
            }
            if options.get('only') is not None:
                nested_options['only'] = options['only']
            if options.get('exclude') is not None:
                nested_options['exclude'] = options['exclude']
                if options.get('only') is not None:
                    allowed_fields = set(options['only'])
                    nested_options['exclude'] = tuple(
                        field_name
                        for field_name in nested_options['exclude']
                        if field_name in allowed_fields
                    )
            nested_field = fields.Nested(schema, **nested_options)
            result[name] = self._configure_field(nested_field, options)
        return result

    def _build_nested_schema(self, options):
        nested_options = {
            key: options[key]
            for key in (
                'exclude', 'only', 'dump_only', 'load_only', 'dump_fields',
                'depth', 'relationships', 'custom_fields', 'methods',
                'custom_validators', 'schema_validator', 'schema_name',
                'auto_relationships',
                'relationship_exclude', 'exclude_from_relationship',
                'relationship_only', 'include_relationships',
                'exclude_relationships', 'include_rel_only', 'rel_exclude',
            )
            if key in options
        }
        # Relationship-level ``dump_only=True`` / ``load_only=True`` are field
        # flags (handled by _configure_field), not schema field lists — don't
        # propagate booleans into the nested SchemaBuilder.
        for _flag in ('dump_only', 'load_only', 'dump_fields'):
            if isinstance(nested_options.get(_flag), bool):
                nested_options.pop(_flag, None)
        if nested_options.get('only') and nested_options.get('exclude'):
            allowed_fields = set(nested_options['only'])
            nested_options['exclude'] = tuple(
                field_name
                for field_name in nested_options['exclude']
                if field_name in allowed_fields
            )
        nested_options.update(options.get('schema_options', {}))
        # Propagate explicit ma/session so nested schemas don't fall back
        # to extension resolution mid-build.
        nested_options.setdefault('ma', self._ma)
        nested_options.setdefault('sqla_session', self._sqla_session)
        return SchemaBuilder(options['model'], **nested_options).schema

    @staticmethod
    def _configure_field(field, options):
        """Apply read/write options to a Marshmallow field instance."""
        field_options = {}
        if 'dump_only' in options:
            field_options['dump_only'] = options['dump_only']
        elif 'read_only' in options:
            field_options['dump_only'] = options['read_only']
        elif 'write' in options:
            field_options['dump_only'] = not options['write']

        for option in ('load_only', 'required', 'allow_none'):
            if option in options:
                field_options[option] = options[option]

        for option, value in field_options.items():
            setattr(field, option, value)
        return field

    def _schema_validator(self):
        validators = self.custom_validators
        schema_validator = self.schema_validator
        if not validators and schema_validator is None:
            return None

        def validate_schema(self, data, **kwargs):
            payload = data if isinstance(data, dict) else {}
            for name, validator in validators.items():
                value = payload.get(name)
                try:
                    parameters = signature(validator).bind(value, payload)
                except (TypeError, ValueError):
                    parameters = None
                try:
                    result = (
                        validator(value, payload)
                        if parameters is not None
                        else validator(value)
                    )
                except ValidationError:
                    raise
                if result is False:
                    raise ValidationError({name: ['Invalid value.']})
            if schema_validator is not None:
                # User validators may accept (data) or (data, **kwargs).
                # Probe the signature first so extra marshmallow kwargs
                # (many, partial, ...) don't break single-arg validators
                # like _validate_fitment_years(data).
                try:
                    sig = signature(schema_validator)
                except (TypeError, ValueError):
                    sig = None
                if sig is not None:
                    try:
                        sig.bind(payload, **kwargs)
                    except (TypeError, ValueError):
                        try:
                            sig.bind(payload)
                        except (TypeError, ValueError):
                            pass
                        else:
                            schema_validator(payload)
                            return
                    else:
                        schema_validator(payload, **kwargs)
                        return
                try:
                    schema_validator(payload, **kwargs)
                except TypeError:
                    schema_validator(payload)

        return validate_schema