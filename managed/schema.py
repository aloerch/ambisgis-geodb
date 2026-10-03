# SPDX-License-Identifier: GPL-3.0-or-later
"""Authoritative v1 typed definitions and lossless identity projections."""
import hashlib
import json
import re

RESERVED = {'dataset_id', 'version_group_id', 'version_id', 'fid', 'object_id',
            'feature_revision', 'geom', 'tableoid', 'xmin', 'xmax', 'cmin', 'cmax', 'ctid'}
TYPES = {'string', 'int32', 'int64', 'decimal', 'boolean', 'uuid', 'date', 'timestamp'}
GEOMETRIES = {'Point', 'LineString', 'Polygon', 'MultiPoint', 'MultiLineString', 'MultiPolygon'}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', value):
        raise ValueError('invalid identifier')
    return value


def normalized(value):
    if not isinstance(value, dict) or set(value) != {'schema_version', 'fields', 'geometry'} or type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('unsupported typed definition envelope')
    fields = value['fields']
    if not isinstance(fields, list) or not 1 <= len(fields) <= 128:
        raise ValueError('expected 1..128 fields')
    names, result = set(), []
    for field in fields:
        if not isinstance(field, dict): raise ValueError('field must be an object')
        name = identifier(field.get('name'))
        if name in names or name in RESERVED: raise ValueError('duplicate/reserved field')
        names.add(name)
        kind = field.get('type')
        if not isinstance(kind,str) or kind not in TYPES or type(field.get('nullable')) is not bool: raise ValueError('invalid field type/nullability')
        keys = {'name', 'type', 'nullable'}
        if kind == 'string':
            keys.add('max_length')
            if type(field.get('max_length')) is not int or not 1 <= field['max_length'] <= 1048576: raise ValueError('invalid length')
        if kind == 'decimal':
            keys.update(('precision', 'scale'))
            if any(type(field.get(k)) is not int for k in ('precision', 'scale')) or not 1 <= field['precision'] <= 38 or not 0 <= field['scale'] <= field['precision']: raise ValueError('invalid exact decimal bounds')
        if set(field) != keys: raise ValueError('unknown/missing field property')
        result.append(dict(field))
    geometry = value['geometry']
    if geometry is not None:
        keys = {'type', 'srid', 'dimensions', 'nullable', 'allow_empty', 'require_valid'}
        if not isinstance(geometry, dict) or set(geometry) != keys: raise ValueError('invalid geometry definition')
        if not isinstance(geometry['type'],str) or geometry['type'] not in GEOMETRIES or geometry['dimensions'] not in ('XY', 'XYZ', 'XYM', 'XYZM'): raise ValueError('invalid geometry type/dimensions')
        if type(geometry['srid']) is not int or not 1 <= geometry['srid'] <= 998999: raise ValueError('invalid SRID')
        if any(type(geometry[k]) is not bool for k in ('nullable', 'allow_empty', 'require_valid')): raise ValueError('invalid geometry policy')
        if not geometry['require_valid']: raise ValueError('invalid geometry acceptance is not supported')
        geometry = dict(geometry)
    return {'schema_version': 1, 'fields': result, 'geometry': geometry}


def canonical(value):
    """v1: normalized explicit properties, UTF-8, sorted keys, ordered fields."""
    return json.dumps(normalized(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def object_id_int32(value):
    if type(value) is not int or not 1 <= value <= 2147483647:
        raise ValueError('OBJECT_ID_COMPATIBILITY_OVERFLOW')
    return value


def columns(value):
    """SQL contains only validated identifiers/enumerated types and integers."""
    value = normalized(value)
    result = []
    for field in value['fields']:
        name, kind = field['name'], field['type']
        quoted = '"' + name + '"'
        sql_type = {'string': 'text', 'int32': 'integer', 'int64': 'bigint', 'decimal': 'numeric',
                    'boolean': 'boolean', 'uuid': 'uuid', 'date': 'date', 'timestamp': 'timestamptz'}[kind]
        part = quoted + ' ' + sql_type + ('' if field['nullable'] else ' NOT NULL')
        if kind == 'string': part += f' CHECK (char_length({quoted}) <= {field["max_length"]})'
        if kind == 'decimal':
            bound = '1' + '0' * (field['precision'] - field['scale'])
            part += f" CHECK ({quoted} <> 'NaN'::numeric AND {quoted}=trunc({quoted},{field['scale']}) AND abs({quoted}) < {bound}::numeric)"
        if kind in ('date', 'timestamp'):
            year_source = quoted if kind == 'date' else quoted + " AT TIME ZONE 'UTC'"
            part += f' CHECK (isfinite({quoted}) AND extract(year FROM {year_source}) BETWEEN 1 AND 9999)'
        result.append(part)
    geometry = value['geometry']
    if geometry:
        dims = {'XY': 0, 'XYZ': 2, 'XYM': 1, 'XYZM': 3}[geometry['dimensions']]
        # Uncoerced geometry plus exact constraints rejects SRID=0 instead of
        # geometry typmod's implicit assignment of the declared SRID.
        part = 'geom public.geometry' + ('' if geometry['nullable'] else ' NOT NULL')
        part += f" CHECK (public.ST_GeometryType(geom)='ST_{geometry['type']}' AND public.ST_SRID(geom)={geometry['srid']} AND public.ST_Zmflag(geom)={dims} AND public.ST_IsValid(geom,1) AND managed.geometry_finite(geom)"
        if not geometry['allow_empty']: part += ' AND NOT public.ST_IsEmpty(geom)'
        result.append(part + ')')
    return result
