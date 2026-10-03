# SPDX-License-Identifier: GPL-3.0-or-later
"""DB-02 real database acceptance on the owned disposable cluster."""
import copy
import unittest
import uuid
import psycopg2
import managed.database as database
from test_managed_database import ManagedDatabaseTests, connect


def definition():
    return {"schema_version":2,"geometry":None,"fields":[
        {"name":"kind","type":"int32","nullable":False,"default":1},
        {"name":"label","type":"string","max_length":40,"nullable":False,"default":"plain"},
        {"name":"amount","type":"int32","nullable":False,"default":5,"domain":{"kind":"range","min":0,"max":20}},
        {"name":"limit_value","type":"int32","nullable":False,"default":10},
        {"name":"status","type":"string","max_length":20,"nullable":True,"default":"new","domain":{"kind":"coded","values":["new","old"]}}],
        "subtypes":{"field":"kind","variants":[
            {"code":1,"defaults":{},"domains":{}},
            {"code":2,"defaults":{"label":"special","amount":8},"domains":{"amount":{"kind":"range","min":6,"max":12}}}]},
        "relationships":[],"rules":[{"name":"amount_limit","kind":"compare","field":"amount","operator":"le","operand":{"field":"limit_value"}}]}


class SchemaRulesDatabaseTests(ManagedDatabaseTests):
    # Inherited DB01 cases are loaded separately, not duplicated in this suite.
    def row2(self, values=None):
        return {"fid":str(uuid.uuid4()),"values":{} if values is None else values}

    def test_defaults_domains_subtypes_and_atomic_rejection(self):
        d=self.dataset(definition());row=self.row2({"kind":2})
        database.apply_rows(self.connection,d,[row]);d=self.refresh(d)
        t=database.table(d["dataset_id"])
        self.assertEqual(self.query(f"SELECT kind,label,amount,status FROM managed.\"{t}\""),[(2,"special",8,"new")])
        for values in ({"kind":3},{"kind":2,"amount":5},{"amount":21},{"status":"other"},{"amount":11},{"amount":None}):
            self.rejects(d,[self.row2(),self.row2(values)])
        explicit=self.row2({"status":None})
        database.apply_rows(self.connection,self.refresh(d),[explicit])
        self.assertEqual(self.query(f"SELECT status FROM managed.\"{t}\" WHERE fid=%s",(explicit["fid"],)),[(None,)])
        self.rejects(d,[row])  # existing full-row update cannot silently reset defaults

    def test_relation_group_atomicity_restrict_cardinality_and_version_scope(self):
        parent=self.dataset({"schema_version":1,"fields":[{"name":"name","type":"string","max_length":20,"nullable":False}],"geometry":None})
        value=definition();value["fields"].append({"name":"parent","type":"uuid","nullable":False})
        value["relationships"]=[{"name":"owner","field":"parent","target_dataset":parent["dataset_id"],"cardinality":"one-to-one","on_delete":"restrict"}]
        child=self.dataset(value,version_group_id=parent["version_group_id"])
        p={"fid":str(uuid.uuid4()),"values":{"name":"parent"}};c=self.row2({"parent":p["fid"]})
        edits=[{"dataset_id":child["dataset_id"],"rows":[c],"replace":False},{"dataset_id":parent["dataset_id"],"rows":[p],"replace":False}]
        database.apply_group(self.connection,self.refresh(parent),edits)
        self.rejects(child,[self.row2({"parent":p["fid"]}),self.row2({"parent":p["fid"]})])
        self.rejects(parent,[])
        before=(self.state(parent),self.state(child))
        invalid=copy.deepcopy(edits);invalid[0]["rows"]=[self.row2({"parent":str(uuid.uuid4())})]
        with self.assertRaises(psycopg2.Error):database.apply_group(self.connection,self.refresh(parent),invalid)
        self.assertEqual(before,(self.state(parent),self.state(child)))
        other=self.dataset()
        with self.assertRaises(psycopg2.Error):database.apply_group(self.connection,self.refresh(parent),[{"dataset_id":other["dataset_id"],"rows":[],"replace":True}])
        database.apply_group(self.connection,self.refresh(parent),[{"dataset_id":parent["dataset_id"],"rows":[],"replace":True},{"dataset_id":child["dataset_id"],"rows":[],"replace":True}])
        self.assertEqual(len(self.mapping(parent)),1);self.assertEqual(len(self.mapping(child)),1)

    def test_retained_schema_revision_and_failed_change_preserve_identity(self):
        d=self.dataset(definition());row=self.row2()
        database.apply_rows(self.connection,d,[row]);d=self.refresh(d);before=self.mapping(d)
        value=definition();value["fields"][2]["domain"]["max"]=9
        changed=database.revise_schema(self.connection,d,value)
        self.assertNotEqual(changed["schema_revision_id"],d["schema_revision_id"])
        self.assertEqual(changed["group_schema_generation"],d["group_schema_generation"]+1)
        self.assertEqual(before,self.mapping(d))
        self.assertEqual(self.query("SELECT count(*) FROM managed.schema_revision WHERE dataset_id=%s",(d["dataset_id"],)),[(2,)])
        state=self.state(d);value["fields"][2]["domain"]["max"]=4
        with self.assertRaises((ValueError,psycopg2.Error)):database.revise_schema(self.connection,changed,value)
        self.assertEqual(state,self.state(d))
        with self.assertRaisesRegex(ValueError,"STALE_SCHEMA"):database.revise_schema(self.connection,d,definition())

    def test_service_cannot_execute_raw_or_schema_functions(self):
        d=self.dataset(definition());writer="rules_"+uuid.uuid4().hex
        self.query(f"CREATE ROLE \"{writer}\"");database.grant_service(self.connection,writer)
        conn=connect()
        try:
            with conn,conn.cursor() as c:c.execute(f"SET ROLE \"{writer}\"")
            database.apply_rows(conn,d,[self.row2()])
            self.assertEqual(self.query("SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='managed' AND has_function_privilege(%s,p.oid,'EXECUTE') ORDER BY p.proname",(writer,)),[("apply_group",),("apply_rows",)])
        finally:conn.close()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(SchemaRulesDatabaseTests(name) for name in SchemaRulesDatabaseTests.__dict__ if name.startswith("test_"))
