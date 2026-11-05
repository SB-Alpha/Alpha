#!/bin/bash
# 목적지(Cloud SQL 역할): 빈 DB와 앱 계정만 만든다. 스키마 준비 계정과 Sink 계정은 작업의 target_prepare가 만든다.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres <<SQL
CREATE DATABASE app;
CREATE ROLE tgt_app LOGIN;
SQL
