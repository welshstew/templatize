# Failover: orders database

1. Check replication lag on the replica:

       psql -h pg-orders-replica.internal -U monitor -c "select now() - pg_last_xact_replay_timestamp();"

2. Promote the replica:

       ssh ops@pg-orders-replica.internal sudo -u postgres pg_ctl promote -D /var/lib/postgresql/16/main

3. Point the app at the new primary: set `DATABASE_HOST=pg-orders-replica.internal`
   in the `orders-api` deployment (namespace `orders-prod`).

Escalation: page the on-call DBA via PagerDuty service P7X2KQ1, or call +44 20 7946 0958.
