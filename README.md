# AIOS
AIOS sets out to be an operating system for language models. Rather than integrating AI into everything we make an easy to use API for interfacing with LLMs directly. This operating system also includes a fully function vector memory and RAG. 



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





## Notes
- Schema name: aios
- No seed data is included
- Application will auto-populate tables on first run
