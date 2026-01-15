# AIOS Sidecar RAG (Structural Geometry Only)

**IMPORTANT — READ BEFORE USING**

This RAG system is **NOT** a memory store  
This RAG system is **NOT** a truth engine  
This RAG system is **NOT** for prompt injection

## What this system does

• Computes semantic similarity geometry  
• Detects structural clustering and divergence  
• Emits **SQL annotations only**  
• Supports narrative/world-split detection  
• Acts as a *sensor*, not an authority  

## What this system must NEVER do

❌ Inject retrieved text into LLM prompts  
❌ Decide truth, canon, or belief  
❌ Override DAG timestamps  
❌ Replace RDF promotion logic  

## Correct data flow

Vectors → similarity  
Similarity → SQL candidates  
SQL → RDF promotion  
RDF → character reasoning  

If you are about to use vector output inside a prompt,
**you are using this system incorrectly**.
