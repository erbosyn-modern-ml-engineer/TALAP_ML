from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import class_mapper, configure_mappers

from talap.db.base import Base
from talap.db.models import (
    CatalogImportStatus,
    ProductIndexingTask,
    ProductIndexingTaskStatus,
)


def test_all_expected_tables_are_registered() -> None:
    import talap.db.models  # noqa: F401  — registers all tables on Base.metadata

    expected = {
        "merchants",
        "products",
        "product_variants",
        "inventory",
        "catalog_imports",
        "catalog_import_errors",
        "product_indexing_tasks",
        "product_embeddings",
        "channel_connections",
        "inbound_events",
        "inbound_messages",
        "message_processing_jobs",
        "whatsapp_delivery_statuses",
    }
    actual = set(Base.metadata.tables)
    assert actual == expected, f"Expected {expected}, got {actual}"


def test_mapper_configuration_succeeds() -> None:
    import talap.db.models  # noqa: F401

    configure_mappers()


def test_catalog_identity_constraints() -> None:
    import talap.db.models  # noqa: F401

    tables = Base.metadata.tables

    # ── Merchants ────────────────────────────────────────────────────
    merchants = tables["merchants"]
    merchant_ucs = _collect_unique_constraints(merchants)
    assert any(_columns_match(uc, {"slug"}) for uc in merchant_ucs), (
        "Expected UNIQUE(slug) on merchants"
    )

    # ── Products ─────────────────────────────────────────────────────
    products = tables["products"]
    product_ucs = _collect_unique_constraints(products)
    assert any(
        _columns_match(uc, {"merchant_id", "merchant_product_key"})
        for uc in product_ucs
    ), "Expected UNIQUE(merchant_id, merchant_product_key) on products"
    assert any(
        _columns_match(uc, {"id", "merchant_id"}) for uc in product_ucs
    ), "Expected UNIQUE(id, merchant_id) on products"

    # ── ProductVariants ──────────────────────────────────────────────
    variants = tables["product_variants"]
    variant_ucs = _collect_unique_constraints(variants)
    assert any(
        _columns_match(uc, {"merchant_id", "merchant_sku"})
        for uc in variant_ucs
    ), "Expected UNIQUE(merchant_id, merchant_sku) on product_variants"
    assert _has_check_constraint(variants, "ck_product_variants_price_kzt_positive")

    # ── Inventory ────────────────────────────────────────────────────
    inventory = tables["inventory"]
    inventory_ucs = _collect_unique_constraints(inventory)
    assert any(
        _columns_match(uc, {"product_variant_id"}) for uc in inventory_ucs
    ), "Expected UNIQUE(product_variant_id) on inventory"
    assert _has_check_constraint(inventory, "ck_inventory_stock_quantity_non_negative")


def test_variant_tenant_integrity_foreign_key() -> None:
    import talap.db.models  # noqa: F401

    variants = Base.metadata.tables["product_variants"]
    composite_fks = [
        c
        for c in variants.constraints
        if isinstance(c, ForeignKeyConstraint)
        and len(c.columns) == 2
    ]

    assert len(composite_fks) >= 1, (
        "Expected at least one composite FK on product_variants"
    )

    found = False
    for fk in composite_fks:
        local_cols = {col.name for col in fk.columns}
        remote_full_refs = [
            (elem.column.table.name, elem.column.name)
            for elem in fk.elements
        ]
        remote_table_cols = {name for _tbl, name in remote_full_refs}
        if local_cols == {"product_id", "merchant_id"} and remote_table_cols == {
            "id",
            "merchant_id",
        }:
            found = True
            break

    assert found, (
        "Missing composite FK (product_id, merchant_id) → products(id, merchant_id)"
    )


def test_import_constraints() -> None:
    import talap.db.models  # noqa: F401

    tables = Base.metadata.tables
    imports = tables["catalog_imports"]

    required_checks = {
        "ck_catalog_imports_total_rows_non_negative",
        "ck_catalog_imports_valid_rows_non_negative",
        "ck_catalog_imports_invalid_rows_non_negative",
        "ck_catalog_imports_created_products_non_negative",
        "ck_catalog_imports_updated_products_non_negative",
        "ck_catalog_imports_created_variants_non_negative",
        "ck_catalog_imports_updated_variants_non_negative",
        "ck_catalog_imports_updated_inventory_rows_non_negative",
    }

    existing_checks = {
        c.name
        for c in imports.constraints
        if isinstance(c, CheckConstraint) and c.name is not None
    }
    assert required_checks == existing_checks, (
        f"Missing check constraints: {required_checks - existing_checks}"
    )

    # Status enum
    status_col = imports.columns["status"]
    assert status_col.type.__class__.__name__ == "ENUM", (
        "status column must use a PostgreSQL ENUM"
    )
    enum_obj = status_col.type
    assert enum_obj.name == "catalog_import_status"
    assert enum_obj.enums == [
        "pending",
        "validating",
        "importing",
        "completed",
        "failed",
    ]
    # Values must come from CatalogImportStatus.value, not enum member names.
    assert enum_obj.values_callable is not None
    # Type creation is deferred to the future Alembic migration (create_type=False),
    # so the schema audit must not expect SQLAlchemy to emit CREATE TYPE itself.
    assert enum_obj.create_type is False


def test_one_to_one_inventory_relationship() -> None:
    import talap.db.models  # noqa: F401

    configure_mappers()

    variant_mapper = class_mapper(
        Base.registry._class_registry["ProductVariant"]
    )
    inventory_rel = variant_mapper.relationships["inventory"]
    assert inventory_rel.uselist is False, (
        "ProductVariant.inventory must be one-to-one (uselist=False)"
    )


def test_enum_contract() -> None:
    actual = [value.value for value in CatalogImportStatus]
    expected = [
        "pending",
        "validating",
        "importing",
        "completed",
        "failed",
    ]
    assert actual == expected, f"Enum values mismatch: {actual}"


def test_product_indexing_task_metadata() -> None:
    import talap.db.models  # noqa: F401

    tables = Base.metadata.tables
    task_table = tables["product_indexing_tasks"]

    # Native PostgreSQL enum: name and values (from value, not member names)
    status_col = task_table.columns["status"]
    assert status_col.type.__class__.__name__ == "ENUM"
    enum_obj = status_col.type
    assert enum_obj.name == "product_indexing_task_status"
    assert enum_obj.enums == ["pending", "processing", "completed", "failed"]
    assert enum_obj.values_callable is not None
    assert enum_obj.create_type is False

    # JSONB changed_fields
    assert task_table.columns["changed_fields"].type.__class__.__name__ == "JSONB"

    # attempts check with a deterministic name
    assert _has_check_constraint(
        task_table,
        "ck_product_indexing_tasks_attempts_non_negative",
    )

    # Composite Product FK (product_id, merchant_id) → products(id, merchant_id)
    composite_fks = [
        c
        for c in task_table.constraints
        if isinstance(c, ForeignKeyConstraint) and len(c.columns) == 2
    ]
    assert len(composite_fks) == 1
    task_fk = composite_fks[0]
    assert {col.name for col in task_fk.columns} == {"product_id", "merchant_id"}
    assert task_fk.name == "fk_product_indexing_tasks_product_merchant"
    assert task_fk.ondelete == "CASCADE"
    remote = [(elem.column.table.name, elem.column.name) for elem in task_fk.elements]
    assert ("products", "id") in remote
    assert ("products", "merchant_id") in remote

    # Required indexes
    index_names = {index.name for index in task_table.indexes}
    assert {
        "ix_product_indexing_tasks_status",
        "ix_product_indexing_tasks_available_at",
        "ix_product_indexing_tasks_product_id",
    } <= index_names

    # Python enum contract
    actual = [value.value for value in ProductIndexingTaskStatus]
    assert actual == ["pending", "processing", "completed", "failed"]


def test_product_indexing_task_relationship_contract() -> None:
    import talap.db.models  # noqa: F401

    configure_mappers()

    rel = class_mapper(Base.registry._class_registry["Product"]).relationships[
        "indexing_tasks"
    ]
    # ORM must rely on the PostgreSQL ON DELETE CASCADE, never null out the
    # non-null composite FK, and never load the task collection on delete.
    assert rel.passive_deletes is True
    assert "delete-orphan" not in rel.cascade
    assert "delete" not in rel.cascade
    assert rel.back_populates == "product"
    assert rel.backref is None
    # one Product -> many ProductIndexingTask rows
    assert rel.uselist is True


def test_product_indexing_task_jsonb_default_contract() -> None:
    import talap.db.models  # noqa: F401

    column = ProductIndexingTask.__table__.c.changed_fields

    # PostgreSQL JSONB column type
    assert column.type.__class__.__name__ == "JSONB"

    # Python default must be a callable factory, never a shared empty list.
    # SQLAlchemy does not apply column defaults until INSERT, so inspect the
    # callable column default and prove it yields a fresh list per call.
    assert column.default is not None
    default_factory = column.default.arg
    assert callable(default_factory)

    first = default_factory(None)
    second = default_factory(None)
    assert first == [] and second == []
    assert first is not second

    # Server default must produce an empty PostgreSQL JSONB array.
    assert column.server_default is not None
    assert "[]" in str(column.server_default.arg)


# ── Audit tests ──────────────────────────────────────────────────────


def test_column_nullability_contract() -> None:
    import talap.db.models  # noqa: F401

    tables = Base.metadata.tables

    # Required non-null columns (representative sample)
    non_null = {
        "merchants": {"slug", "name", "active", "id", "created_at", "updated_at"},
        "products": {
            "merchant_id", "merchant_product_key", "name", "category",
            "description", "active", "id", "created_at", "updated_at",
        },
        "product_variants": {
            "product_id", "merchant_id", "merchant_sku", "price_kzt",
            "active", "id", "created_at", "updated_at",
        },
        "inventory": {"product_variant_id", "stock_quantity", "id", "created_at", "updated_at"},
        "catalog_imports": {
            "merchant_id", "filename", "status",
            "total_rows", "valid_rows", "invalid_rows",
            "created_products", "updated_products",
            "created_variants", "updated_variants",
            "updated_inventory_rows",
            "id", "created_at", "updated_at",
        },
        "catalog_import_errors": {
            "catalog_import_id", "code", "message", "id", "created_at",
        },
        "product_indexing_tasks": {
            "id", "merchant_id", "product_id", "status", "changed_fields",
            "attempts", "available_at", "created_at", "updated_at",
        },
    }

    nullable = {
        "product_variants": {"size", "color", "material", "image_url"},
        "catalog_imports": {"completed_at", "failed_at"},
        "catalog_import_errors": {"row_number", "field", "value"},
        "product_indexing_tasks": {"started_at", "completed_at", "last_error"},
    }

    for tname, cols in non_null.items():
        table = tables[tname]
        for col_name in cols:
            col = table.columns[col_name]
            assert not col.nullable, f"{tname}.{col_name} must be non-null"

    for tname, cols in nullable.items():
        table = tables[tname]
        for col_name in cols:
            col = table.columns[col_name]
            assert col.nullable, f"{tname}.{col_name} must be nullable"


def test_server_default_contract() -> None:
    import talap.db.models  # noqa: F401

    tables = Base.metadata.tables

    # Boolean server defaults + Python defaults
    for tname in ("merchants", "products", "product_variants"):
        col = tables[tname].columns["active"]
        sd = col.server_default
        assert sd is not None, f"{tname}.active must have a server_default"
        assert col.default is not None and col.default.arg is True, (
            f"{tname}.active must have Python default True"
        )

    # Text default
    desc = tables["products"].columns["description"]
    assert desc.server_default is not None, "products.description must have a server_default"
    assert desc.default is not None and desc.default.arg == "", (
        "products.description must have Python default ''"
    )

    # Integer counter defaults
    counter_fields = [
        "total_rows", "valid_rows", "invalid_rows",
        "created_products", "updated_products",
        "created_variants", "updated_variants",
        "updated_inventory_rows",
    ]
    for field in counter_fields:
        col = tables["catalog_imports"].columns[field]
        assert col.server_default is not None, (
            f"catalog_imports.{field} must have a server_default"
        )
        assert not col.nullable, f"catalog_imports.{field} must be non-null"
        assert col.default is not None and col.default.arg == 0, (
            f"catalog_imports.{field} must have Python default 0"
        )

    # Status default
    status_col = tables["catalog_imports"].columns["status"]
    assert status_col.server_default is not None, (
        "catalog_imports.status must have a server_default"
    )
    assert status_col.default is not None, (
        "catalog_imports.status must have a Python ORM default"
    )
    assert status_col.default.arg is CatalogImportStatus.PENDING, (
        "catalog_imports.status Python default must be PENDING"
    )

    # Timestamp defaults
    for tname in tables:
        if tname == "catalog_import_errors":
            col = tables[tname].columns["created_at"]
            assert col.server_default is not None, f"{tname}.created_at must have server_default"
        else:
            for ts_col in ("created_at", "updated_at"):
                col = tables[tname].columns[ts_col]
                assert col.server_default is not None, (
                    f"{tname}.{ts_col} must have a server_default"
                )
                assert not col.nullable, f"{tname}.{ts_col} must be non-null"
            upd = tables[tname].columns["updated_at"]
            assert upd.onupdate is not None, (
                f"{tname}.updated_at must have onupdate=func.now()"
            )


def test_all_constraints_have_unique_non_empty_names() -> None:
    import talap.db.models  # noqa: F401

    names: list[str] = []
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            name = constraint.name
            assert name, f"Constraint on {table.name} has empty name"
            assert not name.endswith("_"), (
                f"Constraint '{name}' on {table.name} ends with underscore"
            )
            names.append(name)

    assert len(names) == len(set(names)), (
        f"Duplicate constraint names: {sorted(names)}"
    )


def test_foreign_key_delete_policies() -> None:
    from sqlalchemy import ForeignKeyConstraint

    import talap.db.models  # noqa: F401

    tables = Base.metadata.tables

    # products.merchant_id → merchants.id (RESTRICT)
    product_fks = _collect_foreign_keys(tables["products"])
    assert any(
        any(
            elem.parent.name == "merchant_id"
            and elem.target_fullname == "merchants.id"
            and elem.ondelete == "RESTRICT"
            for elem in fk.elements
        )
        for fk in product_fks
    ), "products.merchant_id must be ON DELETE RESTRICT"

    # product_variants → products (composite FK, RESTRICT)
    variant_fk = None
    for c in tables["product_variants"].constraints:
        if isinstance(c, ForeignKeyConstraint) and len(c.columns) == 2:
            variant_fk = c
            break
    assert variant_fk is not None
    assert variant_fk.ondelete == "RESTRICT"

    # inventory.product_variant_id → product_variants.id (RESTRICT)
    inv_fks = _collect_foreign_keys(tables["inventory"])
    assert len(inv_fks) >= 1
    assert any(fk.ondelete == "RESTRICT" for fk in inv_fks)

    # catalog_imports.merchant_id → merchants.id (RESTRICT)
    import_fks = _collect_foreign_keys(tables["catalog_imports"])
    assert any(
        any(
            elem.parent.name == "merchant_id"
            and elem.target_fullname == "merchants.id"
            and elem.ondelete == "RESTRICT"
            for elem in fk.elements
        )
        for fk in import_fks
    ), "catalog_imports.merchant_id must be ON DELETE RESTRICT"

    # catalog_import_errors.catalog_import_id → catalog_imports.id (CASCADE)
    err_fks = _collect_foreign_keys(tables["catalog_import_errors"])
    assert len(err_fks) >= 1
    assert any(fk.ondelete == "CASCADE" for fk in err_fks)


def test_postgresql_ddl_compiles() -> None:
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    import talap.db.models  # noqa: F401

    tables = Base.metadata.tables
    ddl = {}
    for name, table in tables.items():
        ddl[name] = str(CreateTable(table).compile(dialect=postgresql.dialect()))

    assert len(ddl) == 13

    # PostgreSQL UUID types on every primary key
    for tname in ddl:
        assert "UUID" in ddl[tname], f"{tname} must use PostgreSQL UUID types"

    # Composite FK in product_variants
    pv_ddl = ddl["product_variants"]
    assert "FOREIGN KEY(product_id, merchant_id)" in pv_ddl, (
        "Missing composite FK in product_variants DDL"
    )

    # Price and stock checks
    assert "CHECK (price_kzt > 0)" in pv_ddl, (
        "Missing price check in product_variants DDL"
    )
    assert "CHECK (stock_quantity >= 0)" in ddl["inventory"], (
        "Missing stock check in inventory DDL"
    )

    # All CatalogImport counter checks
    counter_checks = [
        "total_rows", "valid_rows", "invalid_rows",
        "created_products", "updated_products",
        "created_variants", "updated_variants",
        "updated_inventory_rows",
    ]
    for field in counter_checks:
        assert f"CHECK ({field} >= 0)" in ddl["catalog_imports"], (
            f"Missing check for {field} in catalog_imports DDL"
        )

    # Expected unique constraints
    assert "UNIQUE (slug)" in ddl["merchants"]
    assert "UNIQUE (merchant_id, merchant_product_key)" in ddl["products"]
    assert "UNIQUE (id, merchant_id)" in ddl["products"]
    assert "UNIQUE (merchant_id, merchant_sku)" in ddl["product_variants"]
    assert "UNIQUE (product_variant_id)" in ddl["inventory"]

    # Enum type referenced (create_type=False means no CREATE TYPE is emitted)
    assert "catalog_import_status" in ddl["catalog_imports"], (
        "DDL must reference catalog_import_status enum type"
    )
    assert "CREATE TYPE" not in ddl["catalog_imports"], (
        "create_type=False must prevent SQLAlchemy from emitting CREATE TYPE"
    )


def test_model_timestamp_shape() -> None:
    import talap.db.models  # noqa: F401

    tables = Base.metadata.tables

    # All tables except catalog_import_errors have both created_at and updated_at
    for tname in tables:
        if tname == "catalog_import_errors":
            assert "created_at" in tables[tname].columns
            assert "updated_at" not in tables[tname].columns, (
                "catalog_import_errors must NOT have updated_at"
            )
        else:
            assert "created_at" in tables[tname].columns
            assert "updated_at" in tables[tname].columns

    # Every table has exactly one id
    for tname in tables:
        assert "id" in tables[tname].columns
        # id is primary key
        col = tables[tname].columns["id"]
        assert col.primary_key, f"{tname}.id must be primary key"


def test_relationship_contract() -> None:
    import talap.db.models  # noqa: F401

    configure_mappers()

    def mapper_of(name: str):
        return class_mapper(Base.registry._class_registry[name])

    # Required back_populates pairs; no legacy backref anywhere
    expected_pairs = {
        ("Merchant", "products", "Product", "merchant"),
        ("Product", "variants", "ProductVariant", "product"),
        ("ProductVariant", "inventory", "Inventory", "product_variant"),
        ("Merchant", "catalog_imports", "CatalogImport", "merchant"),
        ("CatalogImport", "errors", "CatalogImportError", "catalog_import"),
        ("Product", "indexing_tasks", "ProductIndexingTask", "product"),
    }
    for cls_name, rel_name, other_cls, other_rel in expected_pairs:
        rel = mapper_of(cls_name).relationships[rel_name]
        assert rel.back_populates == other_rel, (
            f"{cls_name}.{rel_name} must back_populates {other_cls}.{other_rel}"
        )
        assert rel.backref is None, f"{cls_name}.{rel_name} must not use legacy backref"

    # ProductVariant.inventory is one-to-one
    assert mapper_of("ProductVariant").relationships["inventory"].uselist is False

    # No unintended ORM delete cascade except CatalogImport.errors
    for cls_name in ("Merchant", "Product", "ProductVariant", "Inventory"):
        for rel in mapper_of(cls_name).relationships.values():
            assert "delete" not in rel.cascade, (
                f"{cls_name}.{rel.key} must not cascade deletes"
            )
    errors_rel = mapper_of("CatalogImport").relationships["errors"]
    assert "delete" in errors_rel.cascade and "delete-orphan" in errors_rel.cascade, (
        "Only CatalogImport.errors may cascade with delete-orphan"
    )


def test_no_duplicate_or_missing_columns() -> None:
    import talap.db.models  # noqa: F401

    tables = Base.metadata.tables

    expected = {
        "merchants": {"id", "slug", "name", "active", "created_at", "updated_at"},
        "products": {
            "id", "merchant_id", "merchant_product_key", "name", "category",
            "description", "active", "created_at", "updated_at",
        },
        "product_variants": {
            "id", "merchant_id", "product_id", "merchant_sku", "size", "color",
            "material", "price_kzt", "image_url", "active", "created_at", "updated_at",
        },
        "inventory": {
            "id", "product_variant_id", "stock_quantity", "created_at", "updated_at",
        },
        "catalog_imports": {
            "id", "merchant_id", "filename", "status",
            "total_rows", "valid_rows", "invalid_rows",
            "created_products", "updated_products",
            "created_variants", "updated_variants",
            "updated_inventory_rows",
            "created_at", "updated_at", "completed_at", "failed_at",
        },
        "catalog_import_errors": {
            "id", "catalog_import_id", "code", "message",
            "row_number", "field", "value", "created_at",
        },
        "product_indexing_tasks": {
            "id", "merchant_id", "product_id", "status", "changed_fields",
            "attempts", "available_at", "started_at", "completed_at",
            "last_error", "created_at", "updated_at",
        },
    }

    for tname, cols in expected.items():
        table = tables[tname]
        actual = set(table.columns.keys())
        assert actual == cols, f"{tname} columns mismatch: expected {cols}, got {actual}"
        assert len(table.columns) == len(cols), (
            f"{tname} must not declare duplicate column names"
        )


# ── Inbound ingestion models (T-021) ──────────────────────────────────


def test_inbound_ingestion_unique_constraints_exact() -> None:
    import talap.db.models  # noqa: F401

    tables = Base.metadata.tables

    event_ucs = _collect_unique_constraints(tables["inbound_events"])
    assert any(
        uc.name == "uq_inbound_events_connection_channel_payload_sha256"
        and _columns_match(uc, {"connection_id", "channel", "payload_sha256"})
        for uc in event_ucs
    ), "Expected UNIQUE(connection_id, channel, payload_sha256) on inbound_events"

    message_ucs = _collect_unique_constraints(tables["inbound_messages"])
    assert any(
        uc.name == "uq_inbound_messages_connection_channel_external_message_id"
        and _columns_match(
            uc, {"connection_id", "channel", "external_message_id"}
        )
        for uc in message_ucs
    ), "Expected UNIQUE(connection_id, channel, external_message_id)"

    job_ucs = _collect_unique_constraints(tables["message_processing_jobs"])
    assert any(
        uc.name == "uq_message_processing_jobs_message_id"
        and _columns_match(uc, {"message_id"})
        for uc in job_ucs
    ), "Expected UNIQUE(message_id) on message_processing_jobs"

    status_ucs = _collect_unique_constraints(tables["whatsapp_delivery_statuses"])
    assert any(
        uc.name == "uq_whatsapp_delivery_statuses_connection_fingerprint_sha256"
        and _columns_match(uc, {"connection_id", "fingerprint_sha256"})
        for uc in status_ucs
    ), "Expected UNIQUE(connection_id, fingerprint_sha256) on statuses"

    conn_ucs = _collect_unique_constraints(tables["channel_connections"])
    assert any(
        uc.name == "uq_channel_connections_id_channel"
        and _columns_match(uc, {"id", "channel"})
        for uc in conn_ucs
    ), "Expected UNIQUE(id, channel) on channel_connections"


def test_inbound_ingestion_foreign_keys() -> None:
    from sqlalchemy import ForeignKeyConstraint

    import talap.db.models  # noqa: F401

    tables = Base.metadata.tables

    def fk_of(tname: str, name: str) -> ForeignKeyConstraint:
        for c in tables[tname].constraints:
            if isinstance(c, ForeignKeyConstraint) and c.name == name:
                return c
        raise AssertionError(f"Missing FK {name} on {tname}")

    # Composite connection/channel FKs reuse UNIQUE(channel_connections.id, channel)
    for tname, fk_name in (
        ("inbound_events", "fk_inbound_events_connection_channel"),
        ("inbound_messages", "fk_inbound_messages_connection_channel"),
    ):
        fk = fk_of(tname, fk_name)
        assert len(fk.columns) == 2
        assert {col.name for col in fk.columns} == {"connection_id", "channel"}
        assert fk.ondelete == "RESTRICT"
        remote = [
            (elem.column.table.name, elem.column.name) for elem in fk.elements
        ]
        assert ("channel_connections", "id") in remote
        assert ("channel_connections", "channel") in remote

    event_fk = fk_of("inbound_messages", "fk_inbound_messages_inbound_event")
    assert {col.name for col in event_fk.columns} == {"inbound_event_id"}
    assert event_fk.ondelete == "RESTRICT"

    status_event_fk = fk_of(
        "whatsapp_delivery_statuses", "fk_whatsapp_delivery_statuses_inbound_event"
    )
    assert {col.name for col in status_event_fk.columns} == {"inbound_event_id"}
    assert status_event_fk.ondelete == "RESTRICT"

    status_conn_fk = fk_of(
        "whatsapp_delivery_statuses", "fk_whatsapp_delivery_statuses_connection"
    )
    assert {col.name for col in status_conn_fk.columns} == {"connection_id"}
    assert status_conn_fk.ondelete == "RESTRICT"

    job_fk = fk_of("message_processing_jobs", "fk_message_processing_jobs_message")
    assert {col.name for col in job_fk.columns} == {"message_id"}
    assert job_fk.ondelete == "CASCADE"


def test_inbound_ingestion_check_constraints() -> None:
    import talap.db.models  # noqa: F401

    tables = Base.metadata.tables

    expected_checks = {
        "inbound_events": {"ck_inbound_events_payload_sha256_length"},
        "inbound_messages": {
            "ck_inbound_messages_business_scope",
            "ck_inbound_messages_channel_valid",
            "ck_inbound_messages_message_type_valid",
            "ck_inbound_messages_media_size_non_negative",
            "ck_inbound_messages_media_duration_non_negative",
            "ck_inbound_messages_media_checksum_length",
            "ck_inbound_messages_media_columns_consistent",
            "ck_inbound_messages_type_invariant",
        },
        "message_processing_jobs": {
            "ck_message_processing_jobs_attempts_non_negative"
        },
        "whatsapp_delivery_statuses": {
            "ck_whatsapp_delivery_statuses_fingerprint_length",
            "ck_whatsapp_delivery_statuses_status_valid",
        },
        "channel_connections": {
            "ck_channel_connections_channel_valid",
            "ck_channel_connections_name_not_empty",
        },
    }
    for tname, required in expected_checks.items():
        existing = {
            c.name
            for c in tables[tname].constraints
            if isinstance(c, CheckConstraint) and c.name is not None
        }
        assert required <= existing, (
            f"Missing check constraints on {tname}: {required - existing}"
        )


def test_message_processing_job_enum_contract() -> None:
    import talap.db.models  # noqa: F401

    tables = Base.metadata.tables
    status_col = tables["message_processing_jobs"].columns["status"]
    assert status_col.type.__class__.__name__ == "ENUM"
    enum_obj = status_col.type
    assert enum_obj.name == "message_processing_job_status"
    assert enum_obj.enums == ["pending", "processing", "completed", "failed"]
    assert enum_obj.values_callable is not None
    assert enum_obj.create_type is False

    from talap.db.models import MessageProcessingJobStatus

    assert [value.value for value in MessageProcessingJobStatus] == [
        "pending",
        "processing",
        "completed",
        "failed",
    ]


def test_inbound_jsonb_and_timestamptz_contract() -> None:
    import talap.db.models  # noqa: F401

    tables = Base.metadata.tables

    assert tables["inbound_events"].columns["payload_json"].type.__class__.__name__ == "JSONB"
    assert (
        tables["whatsapp_delivery_statuses"].columns["error_codes"].type.__class__.__name__
        == "JSONB"
    )

    for tname, col in (
        ("inbound_events", "received_at"),
        ("inbound_messages", "received_at"),
        ("whatsapp_delivery_statuses", "occurred_at"),
        ("message_processing_jobs", "available_at"),
    ):
        column = tables[tname].columns[col]
        assert column.type.__class__.__name__ == "DateTime"
        assert column.type.timezone is True, f"{tname}.{col} must be timestamptz"

    error_codes = tables["whatsapp_delivery_statuses"].columns["error_codes"]
    assert error_codes.server_default is not None
    assert "[]" in str(error_codes.server_default.arg)

    job_status = tables["message_processing_jobs"].columns["status"]
    assert job_status.server_default is not None
    assert "pending" in str(job_status.server_default.arg)


def test_inbound_one_to_one_processing_job_relationship() -> None:
    import talap.db.models  # noqa: F401

    configure_mappers()

    message_mapper = class_mapper(Base.registry._class_registry["InboundMessage"])
    job_rel = message_mapper.relationships["processing_job"]
    assert job_rel.uselist is False, (
        "InboundMessage.processing_job must be one-to-one (uselist=False)"
    )
    assert job_rel.passive_deletes is True
    assert job_rel.back_populates == "message"
    assert job_rel.backref is None

    job_mapper = class_mapper(Base.registry._class_registry["MessageProcessingJob"])
    message_rel = job_mapper.relationships["message"]
    assert message_rel.passive_deletes is True
    assert message_rel.back_populates == "processing_job"


def test_inbound_relationship_contract() -> None:
    import talap.db.models  # noqa: F401

    configure_mappers()

    def mapper_of(name: str):
        return class_mapper(Base.registry._class_registry[name])

    expected_pairs = {
        ("ChannelConnection", "inbound_events", "InboundEvent", "connection"),
        ("ChannelConnection", "inbound_messages", "InboundMessage", "connection"),
        (
            "ChannelConnection",
            "whatsapp_delivery_statuses",
            "WhatsAppDeliveryStatus",
            "connection",
        ),
        ("InboundEvent", "messages", "InboundMessage", "inbound_event"),
        (
            "InboundEvent",
            "whatsapp_delivery_statuses",
            "WhatsAppDeliveryStatus",
            "inbound_event",
        ),
        ("InboundMessage", "processing_job", "MessageProcessingJob", "message"),
    }
    for cls_name, rel_name, other_cls, other_rel in expected_pairs:
        rel = mapper_of(cls_name).relationships[rel_name]
        assert rel.back_populates == other_rel, (
            f"{cls_name}.{rel_name} must back_populates {other_cls}.{other_rel}"
        )
        assert rel.backref is None, (
            f"{cls_name}.{rel_name} must not use legacy backref"
        )

    for cls_name in (
        "ChannelConnection",
        "InboundEvent",
        "InboundMessage",
        "WhatsAppDeliveryStatus",
    ):
        for rel in mapper_of(cls_name).relationships.values():
            assert "delete" not in rel.cascade, (
                f"{cls_name}.{rel.key} must not cascade deletes"
            )


def test_inbound_ddl_compiles() -> None:
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    import talap.db.models  # noqa: F401

    ddl = {
        name: str(CreateTable(table).compile(dialect=postgresql.dialect()))
        for name, table in Base.metadata.tables.items()
    }

    assert (
        "UNIQUE (connection_id, channel, payload_sha256)"
        in ddl["inbound_events"]
    )
    assert (
        "UNIQUE (connection_id, channel, external_message_id)"
        in ddl["inbound_messages"]
    )
    assert "UNIQUE (message_id)" in ddl["message_processing_jobs"]
    assert (
        "UNIQUE (connection_id, fingerprint_sha256)"
        in ddl["whatsapp_delivery_statuses"]
    )
    assert "UNIQUE (id, channel)" in ddl["channel_connections"]
    assert "JSONB" in ddl["inbound_events"]
    assert "JSONB" in ddl["whatsapp_delivery_statuses"]
    assert "message_processing_job_status" in ddl["message_processing_jobs"]
    assert "CREATE TYPE" not in ddl["message_processing_jobs"]
    assert "CHECK (message_type IN" in ddl["inbound_messages"]
    assert "CHECK (attempts >= 0)" in ddl["message_processing_jobs"]


# ── helpers ─────────────────────────────────────────────────────────────


def _collect_unique_constraints(table) -> list[UniqueConstraint]:
    return [
        c for c in table.constraints if isinstance(c, UniqueConstraint)
    ]


def _columns_match(uc: UniqueConstraint, expected: set[str]) -> bool:
    return {col.name for col in uc.columns} == expected


def _has_check_constraint(table, name: str) -> bool:
    return any(
        isinstance(c, CheckConstraint) and c.name == name
        for c in table.constraints
    )


def _collect_foreign_keys(table):
    from sqlalchemy import ForeignKeyConstraint

    return [
        c for c in table.constraints
        if isinstance(c, ForeignKeyConstraint)
    ]
