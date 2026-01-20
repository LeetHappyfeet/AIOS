Canonical ontology graphs:
- urn:aios:ontology:world
- urn:aios:ontology:contentkind
- urn:aios:ontology:world-asserted

No other ontology graphs are authoritative.



Verify graphs.
```
curl -X POST \
  -H "Content-Type: application/sparql-query" \
  --data '
SELECT DISTINCT ?g WHERE {
  GRAPH ?g { ?s ?p ?o }
}' \
http://localhost:3030/world/sparql
```

```
#loader script
for f in world.ttl world-contentkind.ttl world-asserted.ttl; do
  curl -X POST -H "Content-Type: text/turtle" \
    --data-binary @"$f" \
    "http://localhost:3030/world/data?graph=urn:aios:ontology:${f%.ttl}"
done
```


#if you need to replace a graph.

```
curl -X POST \
  -H "Content-Type: application/sparql-update" \
  --data 'CLEAR GRAPH <urn:aios:ontology:world>' \
  http://localhost:3030/world/update
  ```
  