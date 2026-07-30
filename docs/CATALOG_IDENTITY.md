# TALAP Catalog Identity Contract

## 1. Purpose

This document defines how TALAP identifies merchants, products, variants, imports, and repeated catalog rows.

The rules are intentionally deterministic.

The importer must not use AI, semantic similarity, product names, or SKU-pattern guessing to decide database identity.

---

## 2. Import endpoint ownership

The production import endpoint is merchant-specific:

```text
POST /api/v1/merchants/{merchant_id}/catalog/import
```

Therefore one production import belongs to exactly one Merchant.

The route Merchant is authoritative.

CSV merchant fields are validated against that Merchant; they do not select a different Merchant.

---

## 3. Merchant identity

Stable identity:

```text
Merchant.id
Merchant.slug
```

Human-readable field:

```text
Merchant.name
```

Rules:

* `id` is the internal database identity.
* `slug` is a stable public machine identity.
* `name` may change.
* CSV rows must not create Merchants automatically.
* Importing requires an existing Merchant.
* Every row's `merchant_slug` must match the target Merchant slug.
* `merchant_name` mismatch may produce a warning later, but it must never redirect ownership.

---

## 4. CSV row meaning

Each CSV data row represents:

> One concrete sellable product variant with one SKU, one price, and one stock value.

Examples:

```text
White shirt, height 146
White shirt, height 152
Black backpack
A5 blue notebook
```

The CSV row is not an order, recommendation, embedding, or demand signal.

---

## 5. Merchant SKU

Field:

```text
merchant_sku
```

Meaning:

> A Merchant-controlled stable identifier for one sellable variant.

Rules:

* Required.
* Non-empty.
* Unique inside one Merchant.
* May be reused by another Merchant.
* Case-preserving.
* Must not be generated from the display name.
* Must not be silently lowercased.
* Reimporting the same Merchant SKU refers to the same ProductVariant.
* Changing a SKU creates a new identity unless a future explicit migration feature is used.

Global uniqueness is not required:

```text
Merchant A / SKU-001
Merchant B / SKU-001
```

are different variants.

Database key:

```text
UNIQUE(merchant_id, merchant_sku)
```

---

## 6. Product identity in MVP

The current CSV has no separate product-family key.

Therefore the MVP uses:

```text
merchant_product_key = merchant_sku
```

Consequences:

* every current CSV row creates or updates one Product;
* that Product has one ProductVariant;
* product and variant remain separate relational entities;
* future grouped variants can be added without redesigning search, inventory or handoff tables.

This is an explicit MVP compromise, not an accidental inference.

The importer must not group rows by:

```text
product_name
description
category
material
shared SKU prefix
similar text
embedding similarity
```

Two rows with the same product name but different SKUs remain separate Products in the current MVP.

---

## 7. Future grouped variants

A future catalog format may add:

```text
merchant_product_key
```

Example:

```csv
merchant_product_key,merchant_sku,product_name,size,color
classic-shirt,classic-shirt-146,Белая школьная рубашка,146,white
classic-shirt,classic-shirt-152,Белая школьная рубашка,152,white
classic-shirt,classic-shirt-158,Белая школьная рубашка,158,white
```

Then:

```text
one Product:
merchant_product_key = classic-shirt

three ProductVariants:
classic-shirt-146
classic-shirt-152
classic-shirt-158
```

This field is not required for the current T-015 implementation.

DeepSeek or the importer must not invent it from existing data.

---

## 8. Variant attributes

Current variant attributes:

```text
size
color
material
price_kzt
image_url
active
stock_quantity
```

Rules:

* empty optional values become `null`;
* price must be greater than zero;
* stock must be zero or greater;
* `active=false` means the Merchant does not want the variant recommended;
* `stock_quantity=0` means unavailable for normal recommendation;
* price and stock updates do not change identity.

Changing:

```text
price
stock
active
image
description
```

must update the existing SKU, not create a new SKU.

---

## 9. Product display fields

Fields:

```text
product_name
category
description
```

These fields describe a Product but do not identify it.

Rules:

* a Merchant may rename a Product;
* a category may be corrected;
* a description may be rewritten;
* none of these operations may create a duplicate when the identity key is unchanged.

---

## 10. Import identity

Every upload attempt receives a new:

```text
CatalogImport.id
```

Even if:

* the same file is uploaded twice;
* the filename is unchanged;
* the contents are identical;
* the previous import completed successfully.

Import attempts are historical events.

They are not deduplicated by filename.

---

## 11. Repeated rows inside one CSV

The same `merchant_sku` must not appear more than once in one input CSV.

Reason:

Two rows with the same SKU may contain conflicting:

```text
price
stock
size
color
active status
```

The importer must reject the file with a clear duplicate-SKU error rather than depend on row order.

Required future importer validation:

```text
duplicate merchant_sku in the same file
→ import FAILED
→ no catalog mutation
```

The last row must not silently win.

---

## 12. Merchant validation

Given:

```text
POST /merchants/{merchant_id}/catalog/import
```

the importer performs:

1. Load Merchant by `merchant_id`.
2. Reject when Merchant does not exist.
3. Reject when Merchant is inactive.
4. Check every row's `merchant_slug`.
5. Reject the whole file if any row belongs to another slug.
6. Never create or select a Merchant from CSV values.

A production CSV containing multiple merchant slugs is invalid.

The multi-merchant demo CSV is only test/seed data and must not be uploaded through the normal merchant import endpoint as one file.

A separate demo-seeding script may split it by `merchant_slug`.

---

## 13. Upsert semantics

### First import

Unknown identity:

```text
Product not found
→ create Product

ProductVariant not found
→ create ProductVariant

Inventory not found
→ create Inventory
```

### Repeated import

Known identity:

```text
Product found
→ update descriptive Product fields

ProductVariant found
→ update attributes and price

Inventory found
→ update stock
```

### Required outcome

Reimporting the same CSV must be idempotent regarding entity count:

```text
Product count does not increase
ProductVariant count does not increase
Inventory count does not increase
```

Import history count does increase because each attempt is a separate historical record.

---

## 14. All-or-nothing policy

The MVP does not support partial catalog mutation.

Policy:

```text
parse every row
→ collect every validation error
→ if any error exists, reject the entire import
```

This means:

* one bad row does not hide errors in later rows;
* the user receives a complete error report;
* no Product, ProductVariant or Inventory row is changed;
* the corrected file can be safely uploaded again.

A future partial-import mode requires a separate explicit product decision.

---

## 15. Source metadata

Current demo CSV contains:

```text
source_availability
source_url
source_checked_at
data_mode
```

These fields document the origin of demo rows.

They are not catalog identity.

They must not affect upsert keys.

For production merchant uploads:

* they may be omitted;
* they may be stored later as import metadata;
* they must not override price, stock or Merchant ownership.

---

## 16. Image identity

`image_url` is descriptive data, not identity.

Changing an image URL updates the existing ProductVariant.

The MVP does not download or copy remote images during CSV parsing.

Image validation and storage synchronization are separate future tasks.

---

## 17. Category identity

`category` is a normalized searchable label.

It is not a foreign key in the initial MVP.

Examples:

```text
school_shirt
school_backpack
school_notebook
```

Changing category updates the Product and may require search reindexing.

It must not create a new Product when the Merchant identity key remains unchanged.

A dedicated category taxonomy may be introduced later.

---

## 18. Indexing identity

Embeddings are derived from catalog rows but do not identify catalog entities.

An embedding belongs to a database Product or ProductVariant ID.

If embedding generation fails:

* catalog identity remains valid;
* product data remains stored;
* indexing may be retried;
* a duplicate Product must not be created.

---

## 19. Handoff identity

Future handoffs reference internal immutable IDs:

```text
merchant_id
product_id
product_variant_id
```

They must not rely only on:

```text
product_name
merchant_sku text copied into a message
position in search results
```

Display values may be snapshotted for history, but relational IDs are authoritative.

---

## 20. Contract summary

```text
Merchant identity:
Merchant.id / Merchant.slug

MVP Product identity:
merchant_id + merchant_product_key
where merchant_product_key = merchant_sku

ProductVariant identity:
merchant_id + merchant_sku

Inventory identity:
product_variant_id

Import identity:
CatalogImport.id

Display fields are never identity.
AI output is never identity.
Embeddings are never identity.
Filename is never identity.
Row order is never identity.
```
