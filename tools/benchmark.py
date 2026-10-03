#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Record eager-snapshot cost, not a production capacity claim."""
import argparse
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from prototype.database import Cluster, DEFAULT


def size(db):
    return json.loads(db.sql("SELECT jsonb_object_agg(relname,pg_total_relation_size(oid)) FROM pg_class "
                            "WHERE relnamespace='geodb'::regnamespace AND relkind='r';"))


def timed(db,sql,repeatable=False):
    start=time.monotonic()
    result=db.sql(('BEGIN ISOLATION LEVEL REPEATABLE READ;'+sql+'COMMIT;') if repeatable else sql,timeout=900)
    return result,time.monotonic()-start


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--prefix',required=True,type=Path)
    parser.add_argument('--evidence',required=True,type=Path)
    parser.add_argument('--rows',type=int,nargs='+',default=[100000,1000000])
    parser.add_argument('--branches',type=int,default=10)
    args=parser.parse_args()
    if any(n<=0 or n%2 for n in args.rows) or not 1<=args.branches<=10:
        parser.error('Use positive even row counts and 1..10 branches')
    results={'host':{'platform':platform.platform(),'cpu_count':os.cpu_count(),
                     'memory':next(l.strip() for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemTotal:'))},
             'conditions':'local warm shared runner; 128MB shared_buffers; psql connection startup included; no policy checks; no cold-cache assertion',
             'fixture':'Half assets and half observations; deterministic UUID from md5(dataset || index); 30-character name; numeric(14,3); PointZ EPSG:4326; all synthetic',
             'workloads':[]}
    def save():
        (args.evidence/'benchmark.json').write_text(json.dumps(results,indent=2)+'\n')
    with Cluster(args.prefix,args.evidence) as db:
        for rows in args.rows:
            db.reset()
            item={'group_rows':rows,'layers':2,'branches':args.branches}
            results['workloads'].append(item)
            sql=''
            for dataset in ('assets','observations'):
                sql+=f"INSERT INTO current_{dataset} SELECT '{DEFAULT}',md5('{dataset}'||i)::uuid,lpad(i::text,30,'x'),(i%1000)/10.0,ST_SetSRID(ST_MakePoint((i%30000)/1000.0,(i%10000)/1000.0,i%100),4326) FROM generate_series(1,{rows//2}) i;"
            _,item['fixture_load_seconds']=timed(db,sql)
            db.sql('CHECKPOINT; ANALYZE;')
            item['before_sizes']=size(db)
            wal=db.sql('SELECT pg_current_wal_lsn();')
            branches=[]
            item['branch_creation_seconds']=[]
            for i in range(args.branches):
                branch,elapsed=timed(db,f"SELECT create_branch('benchmark-{i}');",True)
                branches.append(branch)
                item['branch_creation_seconds'].append(elapsed)
                save()
                print(json.dumps({'rows':rows,'branch':i+1,'seconds':elapsed}),flush=True)
            item['after_branch_sizes']=size(db)
            item['branch_storage_amplification']=sum(item['after_branch_sizes'].values())/sum(item['before_sizes'].values())
            item['branch_wal_bytes']=int(db.sql(f"SELECT pg_wal_lsn_diff(pg_current_wal_lsn(),'{wal}');"))
            branch=branches[0]
            query_times=[]
            for i in range(30):
                _,elapsed=timed(db,f"SELECT count(*) FROM (SELECT feature_id FROM current_assets WHERE version_id='{branch}' AND geom && ST_MakeEnvelope(0,0,10,10,4326) LIMIT 1000) q;")
                query_times.append(elapsed)
            item['query_seconds']={'n':30,'median':statistics.median(query_times),'p95':sorted(query_times)[28], 'includes_psql_startup':True}
            edit_times=[]
            for i in range(20):
                _,elapsed=timed(db,f"SELECT edit('{branch}',{i},'assets','update',md5('assets1')::uuid,lpad('1',30,'x'),{i+1},'SRID=4326;POINT Z(1 2 3)');")
                edit_times.append(elapsed)
            item['edit_seconds']={'n':20,'median':statistics.median(edit_times),'p95':sorted(edit_times)[18]}
            plan,item['reconcile_seconds']=timed(db,f"SELECT prepare_reconcile('{branch}');",True)
            save()
            _,item['accept_seconds']=timed(db,f"SELECT accept_reconcile('{plan}');")
            save()
            _,item['post_transaction_seconds']=timed(db,f"SELECT post('{plan}',21,0,gen_random_uuid());")
            item['post_changed_feature_count']=1
            item['after_reconcile_post_sizes']=size(db)
            item['persisted_branch_rows']=int(db.sql("SELECT (SELECT count(*) FROM current_assets)+(SELECT count(*) FROM current_observations);",timeout=900))
            assert item['persisted_branch_rows']==rows*(args.branches+1)
            save()
            print(json.dumps({'rows':rows,'complete':True,'reconcile_seconds':item['reconcile_seconds'],'post_seconds':item['post_transaction_seconds']}),flush=True)


if __name__=='__main__':
    main()
