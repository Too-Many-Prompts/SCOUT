#!/bin/bash
# Runs once inside the container on first start; loads the synthetic test fixture.
set -euo pipefail
export NLS_LANG=AMERICAN_AMERICA.AL32UTF8  # seed contains non-ASCII (e.g. Zoë Müller)
sqlplus -s / as sysdba <<SQL
WHENEVER SQLERROR EXIT FAILURE
ALTER SESSION SET CONTAINER = FREEPDB1;
@/seed/seed.sql ${SCOUT_TEST_READER_PASSWORD}
EXIT
SQL
