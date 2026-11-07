-- Test stack only: second database used by pytest (TEST_DATABASE_URL).
CREATE EXTENSION IF NOT EXISTS vector;
CREATE DATABASE support_test OWNER test;
\c support_test
CREATE EXTENSION IF NOT EXISTS vector;
