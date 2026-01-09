# AIOS
AIOS sets out to be an operating system for language models. Rather than integrating AI into everything we make an easy to use API for interfacing with LLMs directly. This operating system also includes a fully function vector memory and RAG. AIOS proudly does not rely on LLM or current AI architecture. In fact most of what we do is the opposite of what conventions are. 

┌──────────────────────────┐
│        AIOS Kernel       │
│──────────────────────────│
│ Timeline | Memory | DAG  │
│ Worlds   | Claims | RDF  │
└──────────┬───────────────┘
           │
   ┌───────▼────────┐
   │  Plugin Layer  │
   │────────────────│
   │ HP / Mana      │
   │ Inventory      │
   │ Emotions       │
   │ Relationships  │
   │ Quest Log      │
   └───────┬────────┘
           │
   ┌───────▼────────┐
   │  Text UI Bus   │   ← THIS
   └───────┬────────┘
           │
   ┌───────▼────────┐
   │      LLM       │
   │ (user process) │
   └────────────────┘




# AI-OS Database Setup
- PostgreSQL 14+
We use Docker for easy migration and testing.
docker-compose.yml
```
version: "3.9"

services:
  postgres:
    image: postgres:16
    container_name: postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: aios
      POSTGRES_PASSWORD: aios
      POSTGRES_DB: aiosdb
    ports:
      - "5432:5432"
    volumes:
      - ./data:/var/lib/postgresql/data
    shm_size: 1gb

```

## Load schema
Enter your database in the same folder as your schema and run: ```
psql aiosdb < aios_schema.sql```



## Requirements

- fastapi==0.115.6
- uvicorn[standard]==0.32.1
- asyncpg==0.30.0
- pydantic==2.10.3
- python-dotenv==1.0.1
- requests
- selenium
- beautifulsoup4
- lxml
- gradio



## Overview. 


We have have successfully separated:

| Layer              | Purpose                  | Status   |
| ------------------ | ------------------------ | -------- |
| DAG                | Temporal & source truth  | ✅       |
| document_section   | Canonical chunking       | ✅       |
| extracted_sentence | Deterministic text units | ✅       |
| claim_candidate    | Raw observations         | ✅       |
| RDF / belief       | Interpretation           | ⏳ (next) |

Below is a **README-style section** you can drop almost verbatim into your project. It’s written to explain the *what*, *why*, and *where the boundary is*, in a way that will make sense to future contributors (including future-you).

---

##  Knowledge Ingestion & Pre-RDF Processing

This project implements a **two-phase knowledge architecture**:

1. **SQL-based observation intake** (this repository / current phase)
2. **RDF-based belief, world, and identity reasoning** (future phase, in `AIOS/rdf`)

This section documents **Phase 1**, where raw inputs are converted into traceable, neutral knowledge units suitable for later reasoning — *without yet deciding what is true, believed, or real*.

---

##  Design Philosophy

The system makes a strict distinction between:

* **Observation** — what was said or written
* **Belief** — what an agent accepts as true
* **Reality / World** — the context in which a belief is valid

Most systems collapse these layers. AIOS **intentionally separates them**.

The result is a pipeline that can ingest:

* Conflicting news stories
* Fictional worlds
* Personal memories
* Conversations
* Historical records

…without prematurely resolving contradictions.

---

##  The DAG as the Hinge

All inputs enter the system as **DAG nodes** (`aios.dag_node`).

### Sources include:

* Live chat / character interaction
* Web scrapers
* Books, PDFs, knowledge bases
* Logs
* External accumulators

Each input is inserted into a **timeline-scoped DAG** that preserves:

* Temporal order
* Parent–child relationships
* Provenance
* Source identity

The DAG is the **only authoritative structure** in the system.

Everything else is *derived* from it.

---

##  Step-by-Step Breakdown (SQL Layer)

###  DAG Paragraph Nodes → `document_section`

Each paragraph-level DAG node is projected into:

```
aios.document_section
```

This is a **structural projection only**:

* No interpretation
* No parsing
* No logic

Each section:

* Points back to its originating DAG node
* Knows which document it came from
* Preserves paragraph order

This step answers:

> “What are the discrete chunks of text we may want to reason about later?”

---

###  `document_section` → `extracted_sentence`

Each section is deterministically split into sentences and stored in:

```
aios.extracted_sentence
```

This table is **canonical**:

* Sentences are extracted once
* Never re-parsed
* Shared by all downstream processes

Each sentence:

* Has a stable UUID
* Knows its paragraph and document
* Can be independently reasoned about

This step answers:

> “What are the smallest reusable text units we can safely analyze?”

---

###  `extracted_sentence` → `claim_candidate`

Each sentence is converted into a **raw claim candidate**:

```
aios.claim_candidate
```

Important properties:

* One claim per sentence (by design)
* Claims may be incomplete, wrong, or trivial
* Claims are **not yet beliefs**
* Claims default to a **liminal world**

Each claim is linked to:

* Its sentence
* Its document
* Its extraction method
* A confidence placeholder
* Provenance metadata

This step answers:

> “What statements are being made, without deciding whether they are true?”

---

###  Provenance & Liminal World Assignment

Two supporting tables complete Phase 1:

* `claim_provenance`
  Tracks **where a claim came from** (documents, citations, weights)

* `claim_world_assignment`
  Assigns all new claims to a **liminal world** — meaning:

  > *This statement exists, but has not yet been accepted as true anywhere.*

---

##  Where This Phase Ends

At the end of Phase 1, the system has:

* A fully populated DAG
* Canonical sentence storage
* A complete set of raw claims
* Provenance and traceability
* No ontology commitments
* No truth resolution
* No character belief assignment

This is **intentional**.

At this point, the system has built an **observable universe**, not a belief system.

---

##  Where the RDF Phase Begins

All **logic, interpretation, and belief** happens later, in `AIOS/rdf`.

That phase will handle:

* Promoting or demoting claims
* Assigning claims to worlds
* Resolving contradictions
* Modeling character belief and memory
* Tracking belief change over time

Separating these phases keeps the system:

* Auditable
* Reversible
* Multi-world capable
* Safe from silent truth drift

---

##  Why This Matters (Real-World Applications)

###  Conflicting News Narratives

Two articles can say opposite things.

Instead of choosing:

* Both claims are stored
* Each keeps provenance
* Later logic can reason about:

  * Source credibility
  * Corroboration
  * Timeline consistency

No narrative is erased.

---

###  Books & Fiction

Fictional statements are valid **inside their world**.

This system allows:

* Multiple fictional worlds
* Overlapping entities
* Contradictory canons

Without polluting “real-world truth”.

---

###  Lives & Personal Memory

People remember things differently — and change over time.

This architecture supports:

* Conflicting memories
* Emotional reinterpretation
* Memory assistance and augmentation.
* Character-specific truth


Without overwriting history.

---




> **SQL builds the observable universe; RDF builds belief inside it.**

This repository completes the universe-builder.

The next phase decides what it means.



## Notes
- Schema name: aios
- No seed data is included
- Application will auto-populate tables on first run

Run main to host the web server and run supervisor.py to run the main digestion loop. There are several other programs like accumulator that can be used to scrape web pages or ingest documents into the DAG with the same value as AI chat roleplay logs, the difference is that world assignment happens later and all claims extracted are equally
