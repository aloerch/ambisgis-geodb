#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import json
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prototype.database import Cluster


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prefix',required=True,type=Path)
    parser.add_argument('--evidence',required=True,type=Path)
    parser.add_argument('--baseline',action='store_true',help='Prove tests fail without implementation')
    args = parser.parse_args()
    started = time.monotonic()
    suite = unittest.defaultTestLoader.discover('tests',pattern='test_database.py')
    module = sys.modules['test_database']
    with Cluster(args.prefix,args.evidence) as cluster:
        module.CLUSTER = cluster
        module.BASELINE = args.baseline
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {'baseline':args.baseline,'tests':result.testsRun,'failures':len(result.failures),
              'errors':len(result.errors),'skipped':len(result.skipped),'seconds':time.monotonic()-started,
              'passed':result.wasSuccessful()}
    (args.evidence/'acceptance.json').write_text(json.dumps(report,indent=2)+'\n')
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':
    sys.exit(main())
