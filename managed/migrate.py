# SPDX-License-Identifier: GPL-3.0-or-later
"""Checksum-locked, transactional migrations; no implicit prototype adoption."""
import hashlib
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / 'migrations'
LOCK = 714260601


def migrate(connection, directory=MIGRATIONS):
    if connection.autocommit or connection.get_transaction_status()!=0:
        raise ValueError('migration requires dedicated idle transactional connection')
    files = sorted(Path(directory).glob('[0-9][0-9][0-9]_*.sql'))
    if not files or [int(p.name[:3]) for p in files] != list(range(1, len(files)+1)):
        raise ValueError('migration sequence invalid')
    manifests = [(i+1, p.name, hashlib.sha256(p.read_bytes()).hexdigest(), p.read_text()) for i,p in enumerate(files)]
    with connection:
        with connection.cursor() as cursor:
            cursor.execute('SHOW server_encoding')
            if cursor.fetchone() != ('UTF8',): raise ValueError('UTF8_DATABASE_REQUIRED')
            cursor.execute('SET LOCAL lock_timeout=\'5s\'; SET LOCAL statement_timeout=\'30s\'')
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK,))
            cursor.execute("SELECT to_regnamespace('geodb'), to_regnamespace('managed')")
            prototype, existing = cursor.fetchone()
            if prototype is not None: raise ValueError('PROTOTYPE_ADOPTION_UNSUPPORTED')
            cursor.execute("SELECT extnamespace::regnamespace::text FROM pg_extension WHERE extname='postgis'")
            if cursor.fetchone() != ('public',): raise ValueError('owned PostGIS prerequisite missing or relocated')
            if existing is None:
                cursor.execute('CREATE SCHEMA managed; REVOKE ALL ON SCHEMA managed FROM PUBLIC; CREATE TABLE managed.migration_history (version integer PRIMARY KEY, filename text NOT NULL, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT transaction_timestamp())')
            else:
                cursor.execute("SELECT to_regclass('managed.migration_history')")
                if cursor.fetchone()[0] is None: raise ValueError('UNMARKED_SCHEMA_ADOPTION_UNSUPPORTED')
            cursor.execute('SELECT version,filename,sha256 FROM managed.migration_history ORDER BY version')
            applied = cursor.fetchall()
            if existing is not None and not applied: raise ValueError('UNMARKED_SCHEMA_ADOPTION_UNSUPPORTED')
            if applied != [row[:3] for row in manifests[:len(applied)]]: raise ValueError('MIGRATION_IDENTITY_MISMATCH')
            for version,name,digest,source in manifests[len(applied):]:
                cursor.execute(source)
                cursor.execute('INSERT INTO managed.migration_history(version,filename,sha256) VALUES(%s,%s,%s)', (version,name,digest))
    return [{'version':v,'filename':n,'sha256':d} for v,n,d,_ in manifests]
