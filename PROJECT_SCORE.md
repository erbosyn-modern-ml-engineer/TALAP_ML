# TALAP — Project Scope and Implementation Contract

> Filename retained as `PROJECT_SCORE.md` because it already exists in the repository.
> Semantically, this document defines the project **scope**, system boundaries, invariants,
> and MVP acceptance criteria. It is the authoritative product-and-engineering contract
> for the backend and AI implementation.

## 1. Project identity

**Product:** TALAP  
**Backend subsystem:** TALAP Sauda  
**Repository:** `talap-ai-backend`  
**Primary market:** Kazakhstan  
**Primary languages:** Russian and Kazakh  
**Public channels:** one TALAP WhatsApp Business number and one TALAP Telegram bot  
**Runtime LLM:** DeepSeek API  
**Speech recognition:** GigaAM  
**Frontend:** outside this repository; an existing TALAP website already handles business-facing UI

## 2. One-sentence definition

TALAP is a universal commerce assistant inside WhatsApp and Telegram that searches products
across the catalogs of many local businesses, helps the customer choose a suitable option,
answers merchant-specific questions from verified knowledge, prepares a structured handoff
to the selected merchant, and converts unsuccessful searches into measurable local demand.

## 3. Problem being solved

Customers often know what they need but do not know which nearby business has the correct
product, variant, price, or delivery option. They must search several marketplaces, websites,
Instagram pages, and chats manually.

Small businesses have the opposite problem: they own isolated catalogs and see only the
customers who already found them. They do not see aggregated unmet demand across the district.

TALAP connects these sides through one conversational interface:

```text
customer need
→ structured request
→ multi-merchant catalog search
→ recommendation
→ merchant-specific consultation
→ manager handoff
→ completed commercial conversation
```

When no acceptable product exists:

```text
unsuccessful search
→ unmet-demand record
→ similar requests grouped
→ demand signal for businesses
```

## 4. Product model

TALAP does **not** create a separate chatbot for every business.

The system exposes:

- one universal TALAP WhatsApp conversation;
- one universal TALAP Telegram bot;
- one global product index containing catalogs from many merchants;
- merchant-scoped knowledge bases;
- manager contacts for each merchant;
- one handoff mechanism from TALAP to the selected merchant.

At the start of a conversation, no merchant is active.

```text
conversation.active_merchant_id = null
```

After the customer selects a product:

```text
conversation.active_merchant_id = selected_product.merchant_id
```

Only after that selection may the system retrieve delivery, return, payment, sizing,
discount, address, or other knowledge belonging to that merchant.

## 5. Actors

### 5.1 Customer

The customer:

- writes or sends a voice message to TALAP;
- describes a product need in natural Russian, Kazakh, or mixed language;
- answers clarification questions;
- receives a small set of suitable products;
- selects a product and merchant;
- asks merchant-specific questions;
- explicitly agrees before a handoff is created;
- opens a prepared WhatsApp or Telegram conversation with the manager.

### 5.2 Merchant

The merchant uses the existing TALAP website to:

- register business details;
- upload and update a catalog;
- maintain prices and stock;
- upload business knowledge;
- add manager contacts and working hours;
- view handoff requests and private summaries;
- update handoff status;
- view aggregated unmet demand.

### 5.3 Manager

The manager:

- receives a TALAP handoff;
- sees a private AI-generated summary;
- identifies the request through a short handoff code;
- continues the conversation in the merchant's own WhatsApp or Telegram chat;
- confirms operational details that TALAP is not authorized to finalize.

### 5.4 TALAP backend

The backend owns:

- channel webhooks;
- message normalization;
- conversation state;
- DeepSeek orchestration;
- GigaAM transcription;
- catalog ingestion;
- deterministic product filtering;
- hybrid retrieval and reranking;
- merchant-scoped RAG;
- language and output guards;
- handoff generation;
- private manager summaries;
- unmet-demand storage and clustering;
- audit logs and idempotency.

## 6. Supported channels

### 6.1 WhatsApp

TALAP uses one official WhatsApp Business Platform / Cloud API number.

The backend must support:

- webhook verification;
- webhook signature verification;
- incoming text messages;
- incoming audio/voice messages;
- interactive replies when used;
- outbound text;
- outbound buttons or links;
- delivery-status events;
- message idempotency.

### 6.2 Telegram

TALAP uses one Telegram bot.

The backend must support:

- webhook updates;
- webhook secret validation;
- incoming text;
- incoming voice;
- callback queries;
- outbound text;
- inline URL buttons;
- message idempotency.

### 6.3 Unified internal message

Channel-specific payloads must be converted into one internal schema before any AI logic runs.

Required conceptual fields:

```text
channel
external_chat_id
external_user_id
external_message_id
message_type
text
media_reference
received_at
```

Downstream services must not depend on raw Meta or Telegram payload structures.

## 7. Existing website boundary

No new frontend is built in this repository.

The existing TALAP website calls the backend through authenticated REST endpoints.

The website is responsible for:

- merchant-facing forms;
- catalog upload UI;
- knowledge upload UI;
- manager configuration UI;
- handoff dashboard;
- demand-radar presentation.

The backend is responsible for:

- validation;
- persistence;
- catalog normalization;
- indexing jobs;
- API contracts;
- business rules;
- AI and channel execution.

The website must not write directly into AI-owned tables.

Correct flow:

```text
website
→ TALAP REST API
→ Pydantic validation
→ database transaction
→ background indexing job
```

## 8. Catalog model

The catalog is the source of truth for commercial facts.

Required concepts:

- merchant;
- product;
- product variant;
- price;
- stock;
- category;
- attributes;
- image;
- active status;
- source or import metadata.

DeepSeek must never invent or override:

- product existence;
- SKU;
- price;
- stock;
- size;
- color;
- material;
- discount;
- merchant address;
- delivery time;
- return policy.

Price and stock are read from structured storage immediately before a recommendation,
calculation, or handoff.

## 9. Product search

Product search is global across all active merchants.

The search pipeline is:

```text
customer message
→ DeepSeek structured extraction
→ hard SQL filters
→ full-text retrieval
→ vector retrieval
→ deterministic rank fusion
→ reranking
→ top recommendations
```

Hard filters run before the LLM-generated recommendation text.

Typical hard constraints:

- product is active;
- merchant is active;
- variant is active;
- stock is greater than zero;
- category is compatible;
- price does not exceed a strict maximum;
- required size is available;
- required color is available when mandatory.

The reranker may reorder valid candidates, but it may not introduce a product that did not
pass the hard constraints.

## 10. Knowledge RAG

Product retrieval and knowledge retrieval are different systems.

### 10.1 Product retrieval

Used for finding real catalog items.

Sources:

- structured product tables;
- full-text product index;
- product embeddings;
- deterministic filters.

### 10.2 Merchant knowledge retrieval

Used after a merchant has been selected.

Sources may include:

- delivery rules;
- return and exchange rules;
- payment options;
- size guides;
- store addresses;
- working hours;
- FAQ;
- verified discount rules;
- product-care instructions.

Every knowledge query must include:

```text
merchant_id = active_merchant_id
```

Knowledge from two merchants must never be mixed.

If the required information is absent, TALAP must say that the information is not confirmed
and offer a manager handoff. It must not generate a generic policy from model memory.

## 11. DeepSeek responsibilities

DeepSeek may:

- classify customer intent;
- identify the conversation language;
- extract product constraints;
- identify missing information;
- formulate one useful clarification question;
- explain why retrieved products match;
- answer from supplied merchant evidence;
- propose the next allowed action;
- produce a structured private handoff summary.

DeepSeek may not:

- query the database directly;
- execute arbitrary SQL;
- create products;
- change prices or stock;
- create a handoff without backend validation;
- expose private notes to customers;
- decide tenant boundaries;
- send a message without passing output guards;
- claim that payment, delivery, or reservation is complete unless the backend confirms it.

All structured DeepSeek output must pass:

```text
JSON parsing
→ Pydantic validation
→ semantic validation
→ business-rule validation
```

## 12. Conversation behavior

The conversation is controlled by a state machine, not by unrestricted LLM improvisation.

MVP states:

```text
NEW
DISCOVERY
CLARIFICATION
GLOBAL_SEARCH
RECOMMENDATION
MERCHANT_SELECTED
MERCHANT_QA
HANDOFF_CONSENT
HANDOFF_READY
LINK_SENT
CLOSED
ESCALATED
```

Core rules:

- ask one high-value clarification question at a time;
- show no more than three primary recommendations;
- preserve the structured request separately from chat history;
- require explicit customer consent before creating a handoff;
- do not continue autonomous selling after the customer has moved to the manager;
- preserve deterministic state transitions in backend code.

## 13. Voice messages

Voice messages are supported in both WhatsApp and Telegram.

Pipeline:

```text
channel media reference
→ secure download
→ MIME and size validation
→ FFmpeg conversion
→ WAV mono 16 kHz
→ GigaAM transcription
→ effective_text
→ normal text pipeline
```

GigaAM is a transcription layer only. It does not perform product search or sales logic.

Critical values recognized from speech must be confirmed before handoff:

- size;
- quantity;
- price or budget;
- phone number;
- address;
- date.

Original audio should be temporary. The normalized transcript may be stored as part of
the conversation record.

## 14. Language policy

MVP customer languages:

- Russian;
- Kazakh;
- mixed Russian/Kazakh.

The selected conversation language is stored in backend state.

DeepSeek responses must pass a language guard before outbound delivery.

The guard must reject:

- empty responses;
- Chinese Han characters in a Russian or Kazakh conversation;
- raw JSON shown to the customer;
- leaked system instructions;
- private manager notes;
- unknown product IDs;
- unsupported prices or factual claims.

Failure flow:

```text
invalid model output
→ one repair attempt
→ validation again
→ safe fallback or manager escalation
```

No unvalidated LLM text may be sent directly to WhatsApp or Telegram.

## 15. Handoff model

TALAP cannot transfer the existing TALAP chat into a merchant's personal chat.
It creates a structured handoff and a new conversation link.

Handoff prerequisites:

- a merchant is selected;
- a product variant is selected;
- quantity is known;
- current price and stock are rechecked;
- an active manager exists;
- the customer explicitly consents.

The backend creates:

- a handoff record;
- a short handoff code such as `TLP-7K3M`;
- selected-item records;
- a private manager summary;
- a customer-facing prefilled message;
- a WhatsApp or Telegram manager link.

Kazakh customer-facing template:

```text
Сәлеметсіз бе! TALAP арқылы жазып тұрмын.
{product_name}, {variant} бойынша өтінім қалдырдым.
Өтінім коды: {handoff_code}.
```

Russian customer-facing template:

```text
Здравствуйте! Пишу из чата TALAP.
Оставил(а) заявку по товару: {product_name}, {variant}.
Код заявки: {handoff_code}.
```

The prefilled message contains only the minimum context and handoff code.

The private manager summary may contain:

- customer need;
- selected product and variant;
- quantity;
- budget;
- total;
- relevant questions;
- objections;
- requested deadline;
- recommended next action.

Private summaries must never be placed in outbound customer messages or manager-link URLs.

## 16. Unmet demand

When no suitable product exists, TALAP must not hallucinate an answer.

Flow:

```text
no acceptable result
→ offer valid alternatives
→ if alternatives are rejected, save unmet demand
→ group similar requests
→ expose aggregate signal to Demand Radar
```

An unmet-demand record may contain:

- category;
- normalized attributes;
- budget;
- district;
- deadline;
- source channel;
- request embedding;
- creation time.

MVP clustering may use:

- compatible category;
- compatible location;
- compatible required attributes;
- semantic similarity threshold.

A custom trained clustering model is outside MVP.

## 17. Data isolation and privacy

This is a multi-merchant system.

Every merchant-owned entity must include a merchant identifier or be reachable through an
unambiguous merchant relationship.

Required protections:

- merchant-scoped knowledge retrieval;
- no cross-merchant private data;
- encrypted channel credentials;
- webhook signature verification;
- idempotency on inbound messages;
- idempotency on handoff creation;
- structured audit logs;
- no access tokens in logs;
- masked phone numbers in observability tools;
- private notes stored separately from customer-visible messages.

Product search is intentionally global, but merchant knowledge, manager data, internal notes,
and operational settings are not global.

## 18. Reliability invariants

The system must guarantee:

1. The same webhook cannot create two messages.
2. The same confirmed selection cannot create two handoffs.
3. Messages in one conversation are processed in order.
4. Out-of-stock and inactive products are excluded before recommendation.
5. A merchant-specific answer cannot use another merchant's documents.
6. A private summary cannot be delivered to the customer.
7. An LLM response cannot bypass the output guard.
8. A failed external API call cannot silently mark an outbound message as delivered.
9. Critical voice-transcribed values are confirmed before handoff.
10. Every AI action is traceable to a prompt version and model call.

## 19. Repository responsibilities

This repository includes:

- FastAPI API application;
- background worker;
- ASR worker interface;
- PostgreSQL models and migrations;
- Redis queue integration;
- WhatsApp adapter;
- Telegram adapter;
- DeepSeek client and prompts;
- GigaAM adapter;
- catalog ingestion;
- product retrieval;
- knowledge RAG;
- conversation state machine;
- handoff logic;
- unmet-demand logic;
- tests and evaluation fixtures;
- Dockerfiles and Docker Compose configuration.

This repository does not include:

- a new frontend;
- production deployment;
- live Docker launch by this implementation stream;
- customer payment processing;
- courier integration;
- merchant ERP synchronization;
- marketplace scraping;
- a custom foundation model;
- a separate chatbot per merchant;
- automatic transfer of chat history to a merchant's messenger account.

## 20. Docker boundary

Docker configuration must be present for:

- API;
- worker;
- ASR worker;
- PostgreSQL;
- Redis.

Docker is configuration-only for this implementation stage.

We create:

```text
docker/api.Dockerfile
docker/worker.Dockerfile
docker/asr.Dockerfile
docker-compose.yml
```

We do not require:

- building images;
- running `docker compose up`;
- downloading GigaAM checkpoints;
- validating GPU runtime;
- production deployment.

Infrastructure execution belongs to the teammate responsible for deployment.

## 21. MVP end-to-end acceptance scenario

The MVP is complete when the following scenario works:

1. A merchant catalog is uploaded through the existing website.
2. The backend validates and indexes the catalog.
3. A customer sends a Russian or Kazakh text to TALAP in Telegram or WhatsApp.
4. DeepSeek extracts a structured product request.
5. TALAP asks one missing clarification question when necessary.
6. Hard filters exclude invalid products.
7. Retrieval returns real products from several merchants.
8. TALAP presents up to three recommendations.
9. The customer selects one product.
10. Merchant-specific RAG answers a policy or product question.
11. A voice question can be transcribed by GigaAM and processed through the same pipeline.
12. TALAP requests explicit consent for handoff.
13. The backend rechecks product data and creates one handoff.
14. The manager receives a private summary.
15. The customer receives a manager link with a short prefilled message and handoff code.
16. No private summary is visible to the customer.
17. If no product exists, the request is stored as unmet demand.
18. The existing website can retrieve handoffs and demand clusters through REST API.

## 22. Critical acceptance checks

The MVP must have zero known occurrences of:

```text
Chinese output delivered to customer
cross-merchant knowledge leakage
duplicated inbound message
duplicated handoff
invented product price
recommendation of inactive product
recommendation of out-of-stock variant
private manager note exposed to customer
handoff created without customer consent
```

Testing should be small and intentional. The project values a limited set of critical unit,
integration, AI-evaluation, and end-to-end checks over hundreds of shallow tests.

## 23. Engineering principles

1. Deterministic backend rules over LLM improvisation.
2. Structured catalog data over prompt-stuffed catalogs.
3. Hard filters before semantic ranking.
4. Merchant-scoped RAG after merchant selection.
5. One message schema for all channels.
6. One state machine for text and voice.
7. Explicit consent before handoff.
8. LLM output is untrusted until validated.
9. AI-generated code is reviewed before merge.
10. New complexity must demonstrate measurable improvement.

## 24. Change control

This file is authoritative for MVP scope.

Any change that affects one of the following requires an explicit update to this document
before implementation:

- channel model;
- one-bot versus per-merchant-bot architecture;
- merchant isolation;
- product-search ownership;
- RAG boundaries;
- handoff conditions;
- customer-consent rules;
- private-note visibility;
- source of truth for prices or stock;
- supported languages;
- inclusion or exclusion of payment.

Coding agents must not silently expand or reinterpret this scope.
