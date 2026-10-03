# SPDX-License-Identifier: GPL-3.0-or-later
"""Dependency-free semantic examples; these never substitute for database tests."""
from copy import deepcopy
import unittest

from prototype.reference import reconcile


class ReferenceExamples(unittest.TestCase):
    def setUp(self):
        self.feature={'name':'one','value':1,'geom':'canonical-ewkb-z3'}
        self.base={'id':deepcopy(self.feature)}

    def test_target_changes_are_taken_only_when_branch_unchanged(self):
        target={'id':dict(self.feature,value=2)}
        self.assertEqual((target,{}),reconcile(self.base,self.base,target))

    def test_branch_only_change_is_preserved(self):
        branch={'id':dict(self.feature,name='two')}
        self.assertEqual((branch,{}),reconcile(self.base,branch,self.base))

    def test_disjoint_fields_combine(self):
        branch={'id':dict(self.feature,name='two')}
        target={'id':dict(self.feature,value=2)}
        self.assertEqual(({'id':dict(self.feature,name='two',value=2)},{}),reconcile(self.base,branch,target))

    def test_geometry_and_scalar_conflicts_report_both(self):
        branch={'id':dict(self.feature,value=2,geom='z4')}
        target={'id':dict(self.feature,value=3,geom='z5')}
        self.assertEqual(({}, {'id':['value','geom']}),reconcile(self.base,branch,target))

    def test_equal_concurrent_change_coalesces(self):
        both={'id':dict(self.feature,value=3)}
        self.assertEqual((both,{}),reconcile(self.base,both,both))

    def test_delete_and_unchanged_merges_to_absence(self):
        self.assertEqual(({},{}),reconcile(self.base,{},self.base))
        self.assertEqual(({},{}),reconcile(self.base,self.base,{}))

    def test_both_delete_merges_to_absence(self):
        self.assertEqual(({},{}),reconcile(self.base,{},{}))

    def test_delete_update_is_existence_conflict(self):
        changed={'id':dict(self.feature,value=3)}
        self.assertEqual(({}, {'id':['existence']}),reconcile(self.base,{},changed))

    def test_distinct_uuid_inserts_union(self):
        a={'a':self.feature}
        b={'b':dict(self.feature,name='other')}
        self.assertEqual((a|b,{}),reconcile({},a,b))

    def test_different_inserts_same_uuid_do_not_field_merge(self):
        a={'id':dict(self.feature,name='other')}
        b={'id':dict(self.feature,value=3)}
        self.assertEqual(({}, {'id':['existence']}),reconcile({},a,b))

    def test_identical_inserts_can_coalesce(self):
        self.assertEqual((self.base,{}),reconcile({},self.base,self.base))

    def test_output_does_not_alias_input(self):
        result,_=reconcile(self.base,self.base,self.base)
        result['id']['name']='changed'
        self.assertEqual('one',self.base['id']['name'])
