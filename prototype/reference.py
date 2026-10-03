# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure existence/field oracle independent of the database merge function.

Geometry is canonical EWKB hex including SRID/Z; each geometry is one atomic field.
This small interpreter deliberately excludes production authorization and schemas.
"""
from copy import deepcopy


def reconcile(base, ours, theirs):
    result, conflicts = {}, {}
    for identity in sorted(base.keys() | ours.keys() | theirs.keys()):
        b, o, t = (state.get(identity) for state in (base, ours, theirs))
        if b == o:
            merged = t
        elif b == t or o == t:
            merged = o
        elif None in (b, o, t):
            conflicts[identity] = ['existence']
            continue
        else:
            merged, fields = {}, []
            for field in ('name', 'value', 'geom'):
                changed_o, changed_t = o[field] != b[field], t[field] != b[field]
                if changed_o and changed_t and o[field] != t[field]:
                    fields.append(field)
                else:
                    merged[field] = o[field] if changed_o else t[field]
            if fields:
                conflicts[identity] = fields
                continue
        if merged is not None:
            result[identity] = deepcopy(merged)
    return result, conflicts
