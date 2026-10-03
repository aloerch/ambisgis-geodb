#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hash and verify actual DB-01 receipts; never reinterpret a failed run."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def read(path):return json.loads(Path(path).read_text())


def reference(path):
    path=Path(path).resolve()
    return {'path':str(path),'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def report(root):
    positive=root/'acceptance-006';negative=root/'baseline-004';regression=root/'fnd04-regression-001'
    actual=read(positive/'acceptance.json');baseline=read(negative/'acceptance.json')
    prior=read(regression/'acceptance.json');oracle=read(root/'reference-001.json')
    if not actual['passed'] or actual['skipped'] or not actual['source_unchanged'] or not actual['database_stopped']:
        raise ValueError('managed acceptance did not pass completely')
    if baseline['passed'] or not baseline['baseline'] or not baseline['errors'] or baseline['skipped'] or not baseline['database_stopped']:
        raise ValueError('missing actual failing baseline')
    if not prior['passed'] or prior['tests']!=25 or prior['skipped'] or oracle['exit_code']:
        raise ValueError('prototype regression failed')
    inputs=read(positive/'inputs.json');references=[]
    if inputs['working_diff']:raise ValueError('final acceptance source was not a clean checkpoint')
    for directory in (positive,negative):
        result=read(directory/'acceptance.json');selected=read(directory/'inputs.json')
        if reference(directory/'inputs.json')['sha256']!=result['inputs_sha256'] or reference(directory/'tests.log')['sha256']!=result['test_log_sha256']:
            raise ValueError('acceptance receipt digest mismatch')
        if selected['sources']!=inputs['sources']:raise ValueError('baseline/acceptance source differs')
        for relative,digest in selected['sources'].items():
            if reference(directory/'source-snapshot'/relative)['sha256']!=digest or reference(ROOT/relative)['sha256']!=digest:
                raise ValueError('tested source differs')
    if reference(root/'reference-001.log')['sha256']!=oracle['log_sha256']:raise ValueError('oracle log differs')
    for attempt in ('acceptance-001','acceptance-002','acceptance-003','acceptance-004','acceptance-005','acceptance-006','baseline-001','baseline-002','baseline-003','baseline-004','fnd04-regression-001'):
        directory=root/attempt
        for name in ('acceptance.json','inputs.json','runtime.json','fixture-runtime.json','commands.json','job.json','tests.log','managed-export.json','diagnosis-observed.json'):
            if (directory/name).exists():references.append(reference(directory/name))
    references += [reference(root/'reference-001.json'),reference(root/'reference-001.log')]
    probes=[reference(root.parent/'delivery-control/trust'/name/'result.json')
            for name in ('db01-independent-002','db01-independent-005')]
    references += probes
    histories=[{'attempt':name,'actual_result':read(root/name/'acceptance.json')} for name in ('acceptance-001','acceptance-002','acceptance-003','acceptance-004','acceptance-005')]
    return {'schema_version':1,'task':'DB-01','repository':'aloerch/ambisgis-geodb','repository_id':1376927417,
            'scope':'Managed identity and typed schema foundation; no complete editing, policy, named-branch, restore or release acceptance.',
            'base':'b7e5888b11a4b3d2246daeffe0173113f1628f2d','tested_head':inputs['head'],
            'managed_acceptance':actual,'failing_baseline':baseline,'fnd04_regression':prior,
            'reference_oracle':{'tests':12,'exit_code':oracle['exit_code']},'historical_attempts':histories,
            'independent_security_probes':{'original_findings':probes[0],'repaired_source_probe':probes[1],
                'scope':'Separate reviewer actual DB probes; final review receipt/merge remain integrator gates.'},
            'schema_export_contract':'ambisgis-managed-dataset-v1','canonical_encoding':'ambisgis-typed-schema-json-v1',
            'managed_export':reference(positive/'managed-export.json'),'retained_runtime':inputs['runtime'],
            'schema_authority': {'managed_migrations':read(positive/'runtime.json')['managed_migrations'],
                                 'fixture_prototype_hash_scope':'Inherited prototype SQL hash is a fixture source reference only, not managed DDL.'},
            'tested_source_sha256':inputs['sources'],'references':references,'report_recipe':reference(__file__),
            'migration_review':'independent review and protected merge remain separate integrator gates'}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--evidence-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();value=report(a.evidence_root.resolve())
    with a.output.open('x') as f:f.write(json.dumps(value,indent=2,sort_keys=True)+'\n')
