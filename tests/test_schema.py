# SPDX-License-Identifier: GPL-3.0-or-later
import copy
import unittest
from managed.schema import canonical, fingerprint, normalized, object_id_int32
from managed.database import export_identity


DEFINITION = {'schema_version':1,'fields':[
    {'name':'name','type':'string','nullable':True,'max_length':8},
    {'name':'amount','type':'decimal','nullable':False,'precision':12,'scale':3}],
    'geometry':{'type':'Point','srid':4326,'dimensions':'XY','nullable':False,'allow_empty':False,'require_valid':True}}


class SchemaTests(unittest.TestCase):
    def test_canonical_is_versioned_explicit_and_ordered(self):
        reverse = {k:DEFINITION[k] for k in reversed(DEFINITION)}
        self.assertEqual(canonical(DEFINITION),canonical(reverse))
        reordered=copy.deepcopy(DEFINITION);reordered['fields'].reverse()
        self.assertNotEqual(fingerprint(DEFINITION),fingerprint(reordered))
        self.assertEqual(normalized(DEFINITION),DEFINITION)

    def test_rejects_ambiguous_or_executable_definitions(self):
        cases=[]
        for name in ['fid','object_id','version_id','feature_revision','ctid','x; DROP TABLE y','Name','']:
            d=copy.deepcopy(DEFINITION);d['fields'][0]['name']=name;cases.append(d)
        for prop,value in [('max_length',8.0),('max_length',True),('nullable',None),('default','now()')]:
            d=copy.deepcopy(DEFINITION);d['fields'][0][prop]=value;cases.append(d)
        for key in ['geometry','schema_version']:
            d=copy.deepcopy(DEFINITION);del d[key];cases.append(d)
        d=copy.deepcopy(DEFINITION);d['fields'].append(d['fields'][0]);cases.append(d)
        d=copy.deepcopy(DEFINITION);d['fields'][1]['scale']=13;cases.append(d)
        d=copy.deepcopy(DEFINITION);d['geometry']['require_valid']=False;cases.append(d)
        for d in cases:
            with self.subTest(d=d),self.assertRaises(ValueError): normalized(d)

    def test_exact_large_identity_projection(self):
        u='10000000-0000-4000-8000-000000000001'
        self.assertEqual(export_identity(u,9223372036854775807,u)['object_id'],'9223372036854775807')
        self.assertEqual(object_id_int32(2147483647),2147483647)
        for value in [0,-1,2147483648,True,1.0,'1']:
            with self.assertRaises(ValueError): object_id_int32(value)
