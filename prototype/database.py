# SPDX-License-Identifier: GPL-3.0-or-later
"""Only disposable clusters; no DSN argument, network listener, or package fetching."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = '00000000-0000-0000-0000-000000000001'


def literal(value):
    if value is None:
        return 'NULL'
    return "'" + str(value).replace("'", "''") + "'"


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class Cluster:
    def __init__(self, prefix, evidence):
        self.prefix = Path(prefix).resolve()
        self.evidence = Path(evidence).resolve()
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='ambisgis-fnd04-'))
        self.data = self.root / 'data'
        self.socket = self.root / 'socket'
        self.socket.mkdir(mode=0o700)
        (self.root / 'JOB_OWNERSHIP.json').write_text(json.dumps({
            'task': 'FND-04', 'pid': os.getpid(), 'synthetic_only': True}))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('PG')}
        self.env.update(LD_LIBRARY_PATH=str(self.prefix / 'lib'),
                        PGHOST=str(self.socket), PGPORT='55484',
                        PGDATABASE='prototype', PGUSER='prototype_owner')
        self.commands = []
        self.started = False

    def tool(self, name, *args, check=True, **kwargs):
        command = [str(self.prefix / 'bin' / name), *map(str, args)]
        start = time.monotonic()
        completed = subprocess.run(command, env=self.env, capture_output=True,
                                   text=True, timeout=kwargs.pop('timeout', 120), **kwargs)
        self.commands.append({'tool': name, 'seconds': time.monotonic() - start,
                              'exit_code': completed.returncode})
        if check and completed.returncode:
            raise RuntimeError(completed.stderr)
        return completed

    def __enter__(self):
        # Retained build manifests identify the owned source; record actual binaries too.
        manifests = {}
        for component in ('postgresql', 'postgis'):
            path = self.prefix.parent / 'logs' / f'{component}-built.json'
            manifests[component] = {'sha256': sha256(path), 'record': json.loads(path.read_text())}
        expected = {'postgresql': '2ff1375b5dd8bf09d8cb0e795974528180fd75ca',
                    'postgis': '9816f82458db774e62906cfb2c4f01f8b262c862'}
        for component, commit in expected.items():
            if manifests[component]['record']['input']['commit'] != commit:
                raise ValueError('Unexpected owned runtime source identity')
        self.tool('initdb', '-D', self.data, '-A', 'trust', '-U', 'prototype_owner', '--no-locale')
        with (self.data / 'postgresql.conf').open('a') as config:
            config.write(f"\nlisten_addresses = ''\nunix_socket_directories = '{self.socket}'\n"
                         "port = 55484\nshared_buffers = '128MB'\nmax_connections = 20\n")
        self.tool('pg_ctl', '-D', self.data, '-l', self.evidence / 'postgres.log', '-w', 'start')
        self.started = True
        try:
            self.tool('createdb', 'prototype')
            self.sql('CREATE EXTENSION postgis; CREATE ROLE outsider;')
        except BaseException:
            self.tool('pg_ctl', '-D', self.data, '-m', 'fast', '-w', 'stop')
            self.started = False
            raise
        runtime = {'prefix': str(self.prefix), 'job_root': str(self.root), 'owned_builds': manifests,
                   'versions': self.sql('SELECT version(); SELECT postgis_full_version();'),
                   'binaries': {name: sha256(self.prefix / 'bin' / name)
                                for name in ('postgres', 'psql', 'initdb', 'pg_ctl')},
                   'schema_sha256': sha256(ROOT / 'sql/001_prototype.sql')}
        (self.evidence / 'runtime.json').write_text(json.dumps(runtime, indent=2) + '\n')
        return self

    def __exit__(self, *exc):
        if self.started:
            self.tool('pg_ctl', '-D', self.data, '-m', 'fast', '-w', 'stop')
        (self.evidence / 'commands.json').write_text(json.dumps(self.commands, indent=2) + '\n')
        # Retain the stopped job-owned cluster for forensic replay. No user paths are deleted.

    def sql(self, source, error=None, timeout=120):
        completed = self.tool('psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1',
                              input='SET search_path=geodb,public;\n' + source,
                              check=error is None, timeout=timeout)
        if error is not None:
            if completed.returncode == 0 or error not in completed.stderr:
                raise AssertionError(f'Expected {error}: {completed.stdout} {completed.stderr}')
        return completed.stdout.strip()

    def reset(self, baseline=False):
        self.sql('DROP SCHEMA IF EXISTS geodb CASCADE;')
        if not baseline:
            self.sql((ROOT / 'sql/001_prototype.sql').read_text())

    def transaction(self, source):
        return self.sql('BEGIN ISOLATION LEVEL REPEATABLE READ;\n' + source + '\nCOMMIT;')

    def background(self, source, name):
        env = dict(self.env, PGAPPNAME=name)
        process = subprocess.Popen([str(self.prefix / 'bin/psql'), '-X', '-qAt', '-v', 'ON_ERROR_STOP=1'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=env)
        process.stdin.write('SET search_path=geodb,public; SET statement_timeout=30000;\n' + source)
        process.stdin.close()
        process.stdin = None
        return process

    def wait_activity(self, name, wait_event):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.sql('SELECT pid FROM pg_stat_activity WHERE application_name=' + literal(name)
                              + ' AND wait_event=' + literal(wait_event))
            if result:
                return int(result)
            time.sleep(0.02)
        raise AssertionError('Backend did not reach expected PostgreSQL wait event')
