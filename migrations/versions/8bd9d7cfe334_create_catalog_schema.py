"""create catalog schema

Revision ID: 8bd9d7cfe334
Revises:
Create Date: 2026-07-31 19:21:45.960664

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "8bd9d7cfe334"
down_revision: str | None = None
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None

# The ORM CatalogImport.status column uses create_type=False, so the
# migration owns the catalog_import_status PostgreSQL enum lifecycle.
catalog_import_status_enum = postgresql.ENUM(
    "pending",
    "validating",
    "importing",
    "completed",
    "failed",
    name="catalog_import_status",
    create_type=False,
)


def upgrade() -> None:
    catalog_import_status_enum.create(op.get_bind(), checkfirst=True)

    # ── merchants ─────────────────────────────────────────────────────
    op.create_table(
        "merchants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_merchants")),
        sa.UniqueConstraint("slug", name="uq_merchants_slug"),
    )

    # ── products ──────────────────────────────────────────────────────
    op.create_table(
        "products",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_product_key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("description", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_products_merchant_id_merchants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint(
            "merchant_id",
            "merchant_product_key",
            name="uq_products_merchant_product_key",
        ),
        sa.UniqueConstraint("id", "merchant_id", name="uq_products_id_merchant_id"),
    )

    # ── product_variants ──────────────────────────────────────────────
    op.create_table(
        "product_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_sku", sa.String(), nullable=False),
        sa.Column("size", sa.String(), nullable=True),
        sa.Column("color", sa.String(), nullable=True),
        sa.Column("material", sa.String(), nullable=True),
        sa.Column("price_kzt", sa.Integer(), nullable=False),
        sa.Column("image_url", sa.String(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("price_kzt > 0", name="ck_product_variants_price_kzt_positive"),
        sa.ForeignKeyConstraint(
            ["product_id", "merchant_id"],
            ["products.id", "products.merchant_id"],
            name=op.f("fk_product_variants_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_variants")),
        sa.UniqueConstraint(
            "merchant_id",
            "merchant_sku",
            name="uq_product_variants_merchant_sku",
        ),
    )

    # ── inventory ─────────────────────────────────────────────────────
    op.create_table(
        "inventory",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_variant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stock_quantity", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "stock_quantity >= 0",
            name="ck_inventory_stock_quantity_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["product_variant_id"],
            ["product_variants.id"],
            name=op.f("fk_inventory_product_variant_id_product_variants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory")),
        sa.UniqueConstraint("product_variant_id", name="uq_inventory_product_variant_id"),
    )

    # ── catalog_imports ───────────────────────────────────────────────
    op.create_table(
        "catalog_imports",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column(
            "status",
            catalog_import_status_enum,
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "total_rows",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "valid_rows",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "invalid_rows",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_products",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "updated_products",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_variants",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "updated_variants",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "updated_inventory_rows",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "total_rows >= 0",
            name="ck_catalog_imports_total_rows_non_negative",
        ),
        sa.CheckConstraint(
            "valid_rows >= 0",
            name="ck_catalog_imports_valid_rows_non_negative",
        ),
        sa.CheckConstraint(
            "invalid_rows >= 0",
            name="ck_catalog_imports_invalid_rows_non_negative",
        ),
        sa.CheckConstraint(
            "created_products >= 0",
            name="ck_catalog_imports_created_products_non_negative",
        ),
        sa.CheckConstraint(
            "updated_products >= 0",
            name="ck_catalog_imports_updated_products_non_negative",
        ),
        sa.CheckConstraint(
            "created_variants >= 0",
            name="ck_catalog_imports_created_variants_non_negative",
        ),
        sa.CheckConstraint(
            "updated_variants >= 0",
            name="ck_catalog_imports_updated_variants_non_negative",
        ),
        sa.CheckConstraint(
            "updated_inventory_rows >= 0",
            name="ck_catalog_imports_updated_inventory_rows_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_catalog_imports_merchant_id_merchants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_imports")),
    )

    # ── catalog_import_errors ─────────────────────────────────────────
    op.create_table(
        "catalog_import_errors",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("catalog_import_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=True),
        sa.Column("field", sa.String(), nullable=True),
        sa.Column("value", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["catalog_import_id"],
            ["catalog_imports.id"],
            name=op.f("fk_catalog_import_errors_catalog_import_id_catalog_imports"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_import_errors")),
    )


def downgrade() -> None:
    op.drop_table("catalog_import_errors")
    op.drop_table("catalog_imports")
    op.drop_table("inventory")
    op.drop_table("product_variants")
    op.drop_table("products")
    op.drop_table("merchants")
    catalog_import_status_enum.drop(op.get_bind(), checkfirst=True)
