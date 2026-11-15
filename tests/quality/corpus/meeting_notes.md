# Architecture review – 4 December 2026

Attendees: Priya Nair, Tom Okoro, Lena Fischer

- Tom presented the move of the Search Service from Elasticsearch 7.17 to OpenSearch 2.18.
- Lena raised concerns about the reindex time for the products-v3 index (about 1.2 TB).
- Decision: migrate in the staging cluster first, target date 15 January 2027.

Actions
- Tom: write the migration runbook (due 11 December)
- Lena: size the new cluster, budget cap $4,000/month
