"""Deterministic canonical product documents for indexing.

T-017B1 only builds the text; embedding and vector storage arrive later.
The builder is pure and deterministic: the same semantic values always
produce exactly the same text. Price, stock, SKU, and customer data are
intentionally absent from the function contract.
"""

from __future__ import annotations

__all__ = ["build_product_index_text"]


def build_product_index_text(
    *,
    name: str,
    category: str,
    description: str,
    material: str | None,
) -> str:
    """Build the canonical text used as the indexing document for a product.

    Format (one label per line, material falls back to an empty value):

        Name: <name>
        Category: <category>
        Description: <description>
        Material: <material or empty>

    Rules:
    - outer whitespace of every field is stripped,
    - CRLF (and lone CR) newlines are normalized to LF,
    - the text is never rewritten by an LLM,
    - no price, stock, SKU, or customer data can appear: the signature has
      no parameters that could carry them.
    """
    clean_name = name.strip()
    clean_category = category.strip()
    clean_description = description.strip()
    clean_material = (material or "").strip()

    document = (
        f"Name: {clean_name}\n"
        f"Category: {clean_category}\n"
        f"Description: {clean_description}\n"
        f"Material: {clean_material}"
    )
    return document.replace("\r\n", "\n").replace("\r", "\n")
