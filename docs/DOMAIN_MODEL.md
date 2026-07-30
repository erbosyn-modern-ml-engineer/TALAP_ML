# TALAP Catalog Domain Model

## 1. Purpose

This document defines the persistent catalog domain used by TALAP before implementation of:

* catalog database models;
* catalog imports;
* catalog REST API;
* product retrieval;
* embedding indexing;
* merchant handoff.

It is authoritative for the catalog milestone.

Coding agents must not silently change the ownership rules, identity keys, relationships, or transaction boundaries defined here.

---

## 2. Current product boundary

TALAP has:

* one universal WhatsApp bot;
* one universal Telegram bot;
* many merchants;
* one global product-search surface;
* a separate private knowledge base for each merchant.

The catalog is uploaded through the existing TALAP website.

The website communicates with the TALAP backend through authenticated REST endpoints.

The website must not write directly to catalog database tables.

Correct flow:

```text
Existing TALAP website
→ TALAP Catalog API
→ validation
→ database transaction
→ indexing event
```

---

## 3. General database conventions

### 3.1 Primary keys

Persistent entities use UUID primary keys.

Application-generated UUIDs are preferred so objects can be created before a database round trip.

### 3.2 Time

All database timestamps are timezone-aware UTC timestamps.

Required common timestamps:

```text
created_at
updated_at
```

Where relevant:

```text
completed_at
failed_at
processed_at
```

### 3.3 Deletion

Catalog entities use soft deletion or activation flags.

The MVP must not physically delete products because existing conversations, recommendations, handoffs, and import history may reference them.

Typical flag:

```text
active: bool
```

### 3.4 Money

Prices are stored as integer Kazakhstani tenge:

```text
price_kzt: integer
```

Floating-point values must not be used for prices.

The MVP does not support fractional tenge.

### 3.5 Stock

Stock is stored as a non-negative integer:

```text
stock_quantity >= 0
```

Stock represents the merchant-provided available quantity.

TALAP does not claim that this value is real-time unless the merchant updates or synchronizes it.

---

## 4. Merchant

A `Merchant` represents one business whose products are searchable through TALAP.

### Fields

```text
id: UUID
slug: string
name: string
active: bool
created_at: datetime
updated_at: datetime
```

### Constraints

```text
Merchant.slug UNIQUE
```

### Rules

* `slug` is a stable machine identifier.
* `name` is human-readable and may change.
* A disabled merchant is excluded from customer search.
* Disabling a merchant does not delete its products or history.
* Merchant-private information must never be retrieved for another merchant.

### Relationships

```text
Merchant
├── Products
├── CatalogImports
├── Managers
├── KnowledgeDocuments
└── Handoffs
```

Only catalog-related relationships are implemented in the current milestone.

---

## 5. Product

A `Product` represents the common searchable product identity owned by one merchant.

Examples:

```text
Белая школьная рубашка Classic
Рюкзак Grizzly Moon
Тетрадь Be Smart Forest
```

### Fields

```text
id: UUID
merchant_id: UUID
merchant_product_key: string
name: string
category: string
description: string
active: bool
created_at: datetime
updated_at: datetime
```

### Constraints

```text
UNIQUE(merchant_id, merchant_product_key)
```

### Rules

* A Product belongs to exactly one Merchant.
* Product identity must not depend on its display name.
* Renaming a product must update the existing Product, not create a duplicate.
* `category` is a normalized machine-readable category.
* Searchable common text belongs to Product:

  * name;
  * category;
  * description.
* Price and stock do not belong to Product because they can differ by variant.

### MVP identity rule

The current CSV does not contain a separate `merchant_product_key`.

For the MVP importer:

```text
merchant_product_key = merchant_sku
```

This intentionally creates one Product per sellable CSV row.

The schema still separates Product from ProductVariant so grouped variants can be added later without redesigning orders, search results, or inventory.

A future catalog version may add an explicit optional:

```text
merchant_product_key
```

At that point several ProductVariants may belong to one Product.

The importer must not infer product grouping from:

* product name;
* description;
* category;
* SKU prefix;
* size;
* color.

Such inference would be unstable and could merge unrelated products.

---

## 6. ProductVariant

A `ProductVariant` represents one concrete sellable merchant SKU.

Examples:

```text
White shirt, height 146
White shirt, height 152
Black backpack
A5 green notebook
```

### Fields

```text
id: UUID
merchant_id: UUID
product_id: UUID
merchant_sku: string
size: string | null
color: string | null
material: string | null
price_kzt: integer
image_url: string | null
active: bool
created_at: datetime
updated_at: datetime
```

### Constraints

```text
UNIQUE(merchant_id, merchant_sku)
```

### Rules

* A ProductVariant belongs to exactly one Product.
* The Product and ProductVariant must belong to the same Merchant.
* `merchant_sku` is the stable external identity supplied by the Merchant.
* `merchant_sku` is case-preserving.
* The importer must not silently lowercase or rename a SKU.
* Price belongs to ProductVariant.
* Size, color and material belong to ProductVariant for the current MVP.
* An inactive variant is excluded from customer recommendations.
* A variant with zero stock is excluded from normal recommendations.

### Search rules

A variant may be returned only when:

```text
merchant.active = true
product.active = true
variant.active = true
inventory.stock_quantity > 0
```

---

## 7. Inventory

`Inventory` stores the merchant-provided stock for one ProductVariant.

### Fields

```text
id: UUID
product_variant_id: UUID
stock_quantity: integer
created_at: datetime
updated_at: datetime
```

### Constraints

```text
Inventory.product_variant_id UNIQUE
stock_quantity >= 0
```

### Relationship

```text
ProductVariant 1 ─── 1 Inventory
```

### Rules

* Every imported ProductVariant must have one Inventory row.
* Missing stock is not treated as unlimited stock.
* The parser requires `stock_quantity`, so importer-created inventory always has an explicit value.
* Updating only inventory must not trigger embedding regeneration.

---

## 8. CatalogImport

A `CatalogImport` represents one attempt to import one CSV file for one Merchant.

### Fields

```text
id: UUID
merchant_id: UUID
filename: string
status: CatalogImportStatus

total_rows: integer
valid_rows: integer
invalid_rows: integer

created_products: integer
updated_products: integer
created_variants: integer
updated_variants: integer
updated_inventory_rows: integer

created_at: datetime
completed_at: datetime | null
failed_at: datetime | null
```

### Status enum

```text
PENDING
VALIDATING
IMPORTING
COMPLETED
FAILED
```

### Allowed transitions

```text
PENDING → VALIDATING
VALIDATING → IMPORTING
VALIDATING → FAILED
IMPORTING → COMPLETED
IMPORTING → FAILED
```

Terminal states:

```text
COMPLETED
FAILED
```

A completed import must not later return to an active state.

### Rules

* One CatalogImport belongs to one Merchant.
* A production import file must contain rows for only that Merchant.
* Import statistics describe the result of this import attempt.
* A failed import remains visible for debugging and user feedback.
* Import history is never overwritten by a later import.

---

## 9. CatalogImportError

A `CatalogImportError` stores a user-readable validation or import error associated with one CatalogImport.

### Fields

```text
id: UUID
catalog_import_id: UUID
code: string
message: string
row_number: integer | null
field: string | null
value: string | null
created_at: datetime
```

### Rules

* File-level errors have `row_number = null`.
* Row-level errors preserve the original CSV row number.
* Error values must be short and safe.
* Full CSV contents must never be stored in one error record.
* Tracebacks and internal database errors must not be shown to the merchant.
* Multiple field errors may belong to one invalid CSV row.

### Relationship

```text
CatalogImport 1 ─── many CatalogImportError
```

---

## 10. Import transaction model

The MVP uses:

```text
full-file validation
+ all-or-nothing catalog mutation
```

### Phase 1: create import record

Create and commit:

```text
CatalogImport(status=PENDING)
```

This ensures the import attempt remains visible even if later processing fails.

### Phase 2: parse and validate

```text
CatalogImport.status = VALIDATING
CSV bytes
→ parse_catalog_csv()
```

If parser errors exist:

```text
save all CatalogImportError rows
status = FAILED
do not mutate Product, ProductVariant or Inventory
```

### Phase 3: merchant validation

Before import:

* Merchant must exist.
* Merchant must be active.
* Every CSV `merchant_slug` must equal the target Merchant slug.
* CSV `merchant_name` is descriptive and must not change Merchant identity.
* A multi-merchant production CSV must be rejected.

### Phase 4: atomic catalog mutation

When the whole file is valid:

```text
status = IMPORTING

BEGIN TRANSACTION

upsert Products
upsert ProductVariants
upsert Inventory

COMMIT

status = COMPLETED
```

If catalog mutation fails:

```text
ROLLBACK catalog mutation
status = FAILED
store a safe import error
```

The system must never leave half of a catalog update committed.

---

## 11. Upsert identity

### Product upsert key

```text
merchant_id + merchant_product_key
```

For current MVP:

```text
merchant_product_key = merchant_sku
```

### Product fields updated on conflict

```text
name
category
description
active
updated_at
```

### ProductVariant upsert key

```text
merchant_id + merchant_sku
```

### ProductVariant fields updated on conflict

```text
product_id
size
color
material
price_kzt
image_url
active
updated_at
```

### Inventory upsert key

```text
product_variant_id
```

### Inventory fields updated on conflict

```text
stock_quantity
updated_at
```

A repeated import of the same merchant SKU must update existing rows instead of creating duplicates.

---

## 12. Search-index responsibility

The relational catalog is the source of truth.

Embeddings are derived data.

The source of truth for:

```text
price
stock
active status
SKU
variant attributes
merchant ownership
```

is PostgreSQL, not the vector index and not DeepSeek.

Fields that may affect searchable text:

```text
product.name
product.description
product.category
variant.color
variant.material
variant.size
```

Changes to those fields may create an indexing event.

Changes only to:

```text
price_kzt
stock_quantity
```

must not regenerate embeddings.

Price and stock are applied as database filters at query time.

---

## 13. Tenant isolation

Global customer search may read active products across many Merchants.

Private merchant data is not global.

Merchant-scoped entities include:

```text
CatalogImports
CatalogImportErrors
Managers
KnowledgeDocuments
InternalNotes
Merchant settings
```

All merchant-scoped operations must derive Merchant identity from an authenticated backend context or validated route parameter.

DeepSeek must never supply or override `merchant_id`.

---

## 14. Entities intentionally excluded from this milestone

The current database milestone does not yet implement:

```text
Customer
Conversation
Message
KnowledgeDocument
Embedding
Recommendation
Handoff
DemandRequest
DemandCluster
```

Their future relationships are acknowledged, but they must not be prematurely added to the catalog implementation.

---

## 15. Required database constraints

At minimum:

```text
Merchant.slug UNIQUE

Product:
UNIQUE(merchant_id, merchant_product_key)

ProductVariant:
UNIQUE(merchant_id, merchant_sku)

Inventory:
UNIQUE(product_variant_id)
CHECK(stock_quantity >= 0)

ProductVariant:
CHECK(price_kzt > 0)

CatalogImport counters:
CHECK(all counters >= 0)
```

Foreign keys must prevent cross-merchant product/variant relationships through application validation and appropriate relational constraints.

---

## 16. Implementation acceptance criteria

The database model is acceptable only when:

1. A Merchant can own many Products.
2. A Product can own one or more ProductVariants.
3. Each ProductVariant owns exactly one Inventory row.
4. Reimporting the same SKU does not create duplicate products or variants.
5. Price and stock can be updated independently.
6. Catalog import history remains immutable as historical attempts.
7. Invalid files create errors but do not change catalog data.
8. Products from different Merchants can use the same SKU without conflict.
9. Knowledge or private import information cannot leak across Merchants.
10. No LLM-generated value is trusted as a catalog identity.
