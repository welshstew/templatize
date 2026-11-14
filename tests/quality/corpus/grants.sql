CREATE USER reporting_ro WITH PASSWORD 'Rp7!x2Lq';
GRANT CONNECT ON DATABASE analytics TO reporting_ro;
GRANT USAGE ON SCHEMA sales TO reporting_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA sales TO reporting_ro;
