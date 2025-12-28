# AIOS
AIOS sets out to be an operating system for language models. Rather than integrating AI into everything we make an easy to use API for interfacing with LLMs directly. This operating system also includes a fully function vector memory and RAG. 



# AI-OS Database Setup

## Requirements
- PostgreSQL 14+

## Create database
createdb aiosdb

## Load schema
psql aiosdb < aios_schema.sql

## Notes
- Schema name: aios
- No seed data is included
- Application will auto-populate tables on first run
