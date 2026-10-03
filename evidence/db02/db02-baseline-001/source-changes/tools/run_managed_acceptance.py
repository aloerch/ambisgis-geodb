#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned disposable DB-01 tests; no package fetching or existing database DSN."""
import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from prototype.database import Cluster,sha256


class ManagedCluster(Cluster):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        marker=self.root/'JOB_OWNERSHIP.json'
        record=json.loads(marker.read_text());record.update(task='DB-01',fixture_producer='prototype.database.Cluster')
        marker.write_text(json.dumps(record,indent=2)+'\n')

    def __enter__(self):
        super().__enter__()
        try:
            legacy=self.evidence/'runtime.json';legacy.rename(self.evidence/'fixture-runtime.json')
            record=json.loads((self.evidence/'fixture-runtime.json').read_text())
            record['fixture_prototype_reference_sha256']=record.pop('schema_sha256')
            record.update(task='DB-01',database_encoding=self.sql('SHOW server_encoding;'),
                fixture_runtime_sha256=sha256(self.evidence/'fixture-runtime.json'),
                managed_migrations={p.name:sha256(p) for p in sorted((ROOT/'migrations').glob('*.sql'))},
                schema_scope='Managed migrations under test; inherited prototype SQL hash is a fixture source reference only, not executed managed DDL.')
            (self.evidence/'runtime.json').write_text(json.dumps(record,indent=2,sort_keys=True)+'\n')
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def tool(self,name,*args,**kwargs):
        # Keep the accepted prototype fixture unchanged. Managed Unicode data
        # explicitly requires UTF8 even with the deterministic C locale.
        if name=='initdb':args=(*args,'--encoding=UTF8')
        return super().tool(name,*args,**kwargs)


def verify_runtime(prefix,evidence):
    record=json.loads(evidence.read_text());verified={}
    for item in record['runtime']['artifacts']:
        identity=item['identity'];parts=identity['path'].split('/prefix/',1)
        if len(parts)!=2:continue
        path=prefix/parts[1]
        if not path.resolve().is_relative_to(prefix) or sha256(path)!=identity['sha256']:raise ValueError('owned runtime artifact changed')
        verified[parts[1]]=identity['sha256']
    if not {'bin/postgres','bin/initdb','bin/pg_ctl','bin/psql','lib/postgresql/postgis-3.so'}<=set(verified):raise ValueError('runtime evidence missing required artifacts')
    return {'retained_receipt':str(evidence),'sha256':sha256(evidence),'verified_artifacts':verified}


def sources():
    return {str(p.relative_to(ROOT)):sha256(p) for directory in ('managed','migrations','tests','prototype','tools')
            for p in sorted((ROOT/directory).rglob('*')) if p.is_file() and p.suffix in ('.py','.sql')}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--prefix',type=Path,required=True)
    parser.add_argument('--runtime-evidence',type=Path,required=True);parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--baseline',action='store_true');parser.add_argument('--schema-rules',action='store_true');args=parser.parse_args()
    args.prefix=args.prefix.resolve();args.evidence=args.evidence.resolve();args.evidence.mkdir(parents=True,exist_ok=False,mode=0o700)
    before=sources();runtime=verify_runtime(args.prefix,args.runtime_evidence.resolve())
    for relative,digest in before.items():
        target=args.evidence/'source-snapshot'/relative;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/relative,target)
        if sha256(target)!=digest:raise ValueError('source changed during snapshot')
    provenance={'task':'DB-02' if args.schema_rules else 'DB-01','head':subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip(),
                'working_diff':subprocess.check_output(['git','-C',str(ROOT),'status','--porcelain'],text=True),
                'sources':before,'runtime':runtime,'python':sys.executable,'argv':sys.argv,'baseline':args.baseline}
    (args.evidence/'inputs.json').write_text(json.dumps(provenance,indent=2,sort_keys=True)+'\n')
    loader=unittest.TestLoader();suite=unittest.TestSuite()
    for pattern in (('test_schema_rules_database.py',) if args.schema_rules else ('test_schema.py','test_managed_database.py')):suite.addTests(loader.discover(str(ROOT/'tests'),pattern=pattern))
    module=sys.modules['test_managed_database'];module.BASELINE=args.baseline
    start=time.monotonic();cluster=ManagedCluster(args.prefix,args.evidence)
    with cluster:
        # Reuse the accepted no-TCP, private-socket fixture unchanged. This
        # receipt identifies this new DB-01 invocation, not old FND04 evidence.
        (args.evidence/'job.json').write_text(json.dumps({'task':'DB-01','synthetic_only':True,'pid':os.getpid(),'cluster':str(cluster.root),'socket':str(cluster.socket)},indent=2)+'\n')
        module.CLUSTER=cluster
        with (args.evidence/'tests.log').open('x') as log:
            result=unittest.TextTestRunner(verbosity=2,stream=log).run(suite)
    after=sources();unchanged=before==after
    stopped=not (cluster.data/'postmaster.pid').exists()
    report={'task':'DB-02' if args.schema_rules else 'DB-01','baseline':args.baseline,'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
            'skipped':len(result.skipped),'seconds':time.monotonic()-start,'source_unchanged':unchanged,'database_stopped':stopped,
            'passed':result.wasSuccessful() and unchanged and stopped,'inputs_sha256':sha256(args.evidence/'inputs.json'),
            'test_log_sha256':sha256(args.evidence/'tests.log')}
    (args.evidence/'acceptance.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n');print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
