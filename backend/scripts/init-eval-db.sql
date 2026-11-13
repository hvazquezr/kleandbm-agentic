-- Database for the baseline runner (eval/baseline). Migrated to head on each run; every run
-- creates its own tenant and project, so results from earlier runs stay inspectable.
-- Run as a superuser (pgvector needs one): psql -d postgres -f scripts/init-eval-db.sql
CREATE DATABASE kleandbm_eval OWNER kleandbm;
\c kleandbm_eval
CREATE EXTENSION IF NOT EXISTS vector;
