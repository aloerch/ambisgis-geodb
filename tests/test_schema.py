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


class SchemaRulesTests(unittest.TestCase):
    def definition(self):
        return {'schema_version':2,'fields':[
            {'name':'kind','type':'int32','nullable':False,'default':1},
            {'name':'value','type':'decimal','nullable':False,'precision':38,'scale':2,
             'default':'999999999999999999999999999999999999.99',
             'domain':{'kind':'range','min':'0','max':'999999999999999999999999999999999999.99'}}],
            'geometry':None,'subtypes':None,'relationships':[],'rules':[]}

    def test_canonical_v2_preserves_exact_literals_and_changes_rule_hash(self):
        value=self.definition();self.assertEqual(normalized(value),value)
        self.assertEqual(canonical(value),canonical(dict(reversed(list(value.items())))))
        modified=copy.deepcopy(value);modified['rules']=[{'name':'nonnegative','kind':'compare','field':'value','operator':'ge','operand':{'literal':'0'}}]
        self.assertNotEqual(fingerprint(value),fingerprint(modified))

    def test_typed_defaults_domains_and_nonexecutable_rules(self):
        value=self.definition()
        cases=[]
        for v in (1,1.0,True,'NaN','Infinity','1e3','1.123',None):
            d=copy.deepcopy(value);d['fields'][1]['default']=v;cases.append(d)
        for dom in ({'kind':'sql','expression':'SELECT 1'},
                    {'kind':'range','min':'9','max':'1'},
                    {'kind':'coded','values':['1','1.0']},
                    {'kind':'coded','values':[]}):
            d=copy.deepcopy(value);d['fields'][1]['domain']=dom;cases.append(d)
        for operand in ({'sql':'SELECT 1'},{'python':'print(1)'},{'field':'kind'},{'literal':1}):
            d=copy.deepcopy(value);d['rules']=[{'name':'rule','kind':'compare','field':'value','operator':'eq','operand':operand}];cases.append(d)
        for op in ('execute','like','regex',None,[]):
            d=copy.deepcopy(value);d['rules']=[{'name':'rule','kind':'compare','field':'value','operator':op,'operand':{'literal':'1'}}];cases.append(d)
        for d in cases:
            with self.subTest(d=d),self.assertRaises(ValueError):normalized(d)

    def test_domain_defaults_subtypes_and_relationship_envelopes_fail_closed(self):
        value=self.definition()
        value['subtypes']={'field':'kind','variants':[{'code':1,'defaults':{},'domains':{}}]}
        self.assertEqual(normalized(value),value)
        cases=[]
        d=copy.deepcopy(value);d['subtypes']['variants'][0]['code']=2;cases.append(d)
        d=copy.deepcopy(value);d['subtypes']['variants'][0]['defaults']={'kind':1};cases.append(d)
        d=copy.deepcopy(value);d['subtypes']['field']=[];cases.append(d)
        d=copy.deepcopy(value);d['relationships']=[{'name':'ref','field':'kind','target_dataset':'10000000-0000-4000-8000-000000000001','cardinality':'one-to-one','on_delete':'cascade'}];cases.append(d)
        for d in cases:
            with self.subTest(d=d),self.assertRaises(ValueError):normalized(d)
