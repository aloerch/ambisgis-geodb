# SPDX-License-Identifier: GPL-3.0-or-later
"""Finite schema-v2 validation and SQL constraint compilation; no expression eval."""
import copy
import datetime
from decimal import Decimal, localcontext
import re
import uuid

SQL_TYPES = {'string':'text','int32':'integer','int64':'bigint','decimal':'numeric',
             'boolean':'boolean','uuid':'uuid','date':'date','timestamp':'timestamptz'}


def scalar(field, value, nullable=True):
    kind=field['type']
    if value is None:
        if nullable and field['nullable']: return None
        raise ValueError('invalid null literal')
    valid=False
    if kind=='string': valid=isinstance(value,str) and len(value)<=field['max_length']
    elif kind=='boolean': valid=type(value) is bool
    elif kind=='int32': valid=type(value) is int and -2147483648<=value<=2147483647
    elif kind in ('int64','decimal'):
        pattern=r'-?(0|[1-9][0-9]*)' + (r'(\.[0-9]+)?' if kind=='decimal' else '')
        valid=isinstance(value,str) and re.fullmatch(pattern,value) is not None
        if valid:
            number=Decimal(value)
            with localcontext() as context:
                context.prec=max(80,len(value)+field.get('scale',0)+4)
                exact=number==number.quantize(Decimal(1).scaleb(-field.get('scale',0)))
            valid=((-9223372036854775808<=number<=9223372036854775807) if kind=='int64' else
                   exact and number.copy_abs()<10**(field['precision']-field['scale']))
    elif kind=='uuid':
        valid=isinstance(value,str) and re.fullmatch(r'[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',value) is not None
    elif kind in ('date','timestamp'):
        pattern=(r'[0-9]{4}-[0-9]{2}-[0-9]{2}' if kind=='date' else
                 r'[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt]([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{1,6})?([Zz]|[+-]([01][0-9]|2[0-3]):[0-5][0-9])')
        valid=isinstance(value,str) and re.fullmatch(pattern,value) is not None
        if valid:
            try:
                parsed=(datetime.date.fromisoformat(value) if kind=='date' else
                        datetime.datetime.fromisoformat(value.replace('t','T').replace('z','Z')))
                if kind=='timestamp': parsed.astimezone(datetime.timezone.utc)
            except (ValueError,OverflowError):valid=False
    if not valid: raise ValueError('invalid typed literal')
    return value


def comparable(field,value):
    if field['type'] in ('decimal','int64'): return Decimal(value)
    if field['type']=='uuid':return uuid.UUID(value)
    if field['type']=='date':return datetime.date.fromisoformat(value)
    if field['type']=='timestamp':return datetime.datetime.fromisoformat(value.replace('t','T').replace('z','Z'))
    return value


def domain(field,value):
    if not isinstance(value,dict): raise ValueError('invalid domain')
    if value.get('kind')=='coded' and set(value)=={'kind','values'}:
        values=value['values']
        if not isinstance(values,list) or not 1<=len(values)<=256:raise ValueError('invalid coded domain')
        converted=[comparable(field,scalar(field,v,False)) for v in values]
        if len(set(converted))!=len(converted):raise ValueError('duplicate domain code')
    elif value.get('kind')=='range' and set(value)=={'kind','min','max'}:
        if field['type'] not in ('int32','int64','decimal','date','timestamp'):raise ValueError('unsupported range type')
        lo=comparable(field,scalar(field,value['min'],False));hi=comparable(field,scalar(field,value['max'],False))
        if lo>hi:raise ValueError('reversed domain')
    else:raise ValueError('invalid domain')
    return copy.deepcopy(value)


def contains(field,domain_value,value):
    if value is None:return field['nullable']
    v=comparable(field,value)
    if domain_value['kind']=='coded':return v in [comparable(field,x) for x in domain_value['values']]
    return comparable(field,domain_value['min'])<=v<=comparable(field,domain_value['max'])


def normalized_v2(value):
    from .schema import identifier, normalized
    if set(value)!={'schema_version','fields','geometry','subtypes','relationships','rules'} or type(value['schema_version']) is not int:
        raise ValueError('invalid v2 envelope')
    if not isinstance(value['fields'],list) or any(not isinstance(f,dict) for f in value['fields']):raise ValueError('invalid fields')
    base={'schema_version':1,'geometry':value['geometry'],
          'fields':[{k:v for k,v in f.items() if k not in ('default','domain')} for f in value['fields']]}
    normalized(base)
    result=copy.deepcopy(value);fields={f['name']:f for f in result['fields']}
    for field in fields.values():
        if 'default' in field:scalar(field,field['default'])
        if 'domain' in field:domain(field,field['domain'])
    subtype=result['subtypes']
    variants=[{'defaults':{},'domains':{}}]
    if subtype is not None:
        if not isinstance(subtype,dict) or set(subtype)!={'field','variants'} or not isinstance(subtype['field'],str) or subtype['field'] not in fields:raise ValueError('invalid subtype')
        selector=fields[subtype['field']]
        if selector['nullable'] or selector['type'] not in ('int32','string'):raise ValueError('invalid subtype selector')
        variants=subtype['variants']
        if not isinstance(variants,list) or not 1<=len(variants)<=256:raise ValueError('invalid variants')
        codes=[]
        for variant in variants:
            if not isinstance(variant,dict) or set(variant)!={'code','defaults','domains'}:raise ValueError('invalid variant')
            scalar(selector,variant['code'],False);codes.append(variant['code'])
            for key in ('defaults','domains'):
                if not isinstance(variant[key],dict) or not set(variant[key])<=fields.keys() or subtype['field'] in variant[key]:raise ValueError('invalid subtype override')
            for name,v in variant['defaults'].items():scalar(fields[name],v)
            for name,v in variant['domains'].items():domain(fields[name],v)
        if len(set(codes))!=len(codes):raise ValueError('duplicate subtype code')
        if 'default' in selector and selector['default'] not in codes:raise ValueError('invalid selector default')
    for variant in variants:
        for name,field in fields.items():
            default=variant['defaults'].get(name,field.get('default'))
            has_default=name in variant['defaults'] or 'default' in field
            restriction=variant['domains'].get(name,field.get('domain'))
            if has_default and restriction and not contains(field,restriction,default):raise ValueError('default outside domain')
    for collection in ('relationships','rules'):
        if not isinstance(result[collection],list) or len(result[collection])>128:raise ValueError('invalid bounded definitions')
        names=[]
        for item in result[collection]:
            if not isinstance(item,dict):raise ValueError('invalid definition')
            names.append(identifier(item.get('name')))
        if len(set(names))!=len(names):raise ValueError('duplicate definition name')
    for relation in result['relationships']:
        if set(relation)!={'name','field','target_dataset','cardinality','on_delete'} or not isinstance(relation['field'],str) or relation['field'] not in fields:raise ValueError('invalid relationship')
        if fields[relation['field']]['type']!='uuid' or relation['cardinality'] not in ('many-to-one','one-to-one') or relation['on_delete']!='restrict':raise ValueError('unsupported relationship')
        try:relation['target_dataset']=str(uuid.UUID(relation['target_dataset']))
        except (ValueError,AttributeError,TypeError):raise ValueError('invalid relationship target') from None
    for rule in result['rules']:
        if not isinstance(rule.get('field'),str) or rule['field'] not in fields:raise ValueError('unknown rule field')
        field=fields[rule['field']]
        if rule.get('kind')=='required' and set(rule)=={'name','kind','field'}:continue
        if rule.get('kind')!='compare' or set(rule)!={'name','kind','field','operator','operand'}:raise ValueError('unsupported rule')
        operator=rule['operator']
        if operator not in ('eq','ne','lt','le','gt','ge') or (field['type'] in ('string','uuid','boolean') and operator not in ('eq','ne')):raise ValueError('unsupported comparison')
        operand=rule['operand']
        if not isinstance(operand,dict):raise ValueError('invalid operand')
        if set(operand)=={'field'}:
            if not isinstance(operand['field'],str) or operand['field'] not in fields or fields[operand['field']]['type']!=field['type']:raise ValueError('incompatible field comparison')
        elif set(operand)=={'literal'}:scalar(field,operand['literal'],False)
        else:raise ValueError('unsupported operand')
    return result


def literal(field,value):
    if value is None:return 'NULL'
    text=str(value).lower() if type(value) is bool else str(value)
    text=text.replace('\\','\\\\').replace("'","''")
    return "E'"+text+"'::"+SQL_TYPES[field['type']]


def predicate(field,value):
    column='"'+field['name']+'"'
    if value['kind']=='coded':
        return column+' IN ('+','.join(literal(field,x) for x in value['values'])+')'
    return column+' BETWEEN '+literal(field,value['min'])+' AND '+literal(field,value['max'])


def checks(value):
    """Only validated identifiers, typed escaped literals and enumerated SQL."""
    if value['schema_version']==1:return []
    value=normalized_v2(value);fields={f['name']:f for f in value['fields']};result=[]
    subtype=value['subtypes']
    if subtype:
        selector=fields[subtype['field']]
        result.append(predicate(selector,{'kind':'coded','values':[v['code'] for v in subtype['variants']]}))
    for name,field in fields.items():
        if subtype:
            for variant in subtype['variants']:
                restriction=variant['domains'].get(name,field.get('domain'))
                if restriction:result.append('("'+subtype['field']+'" <> '+literal(selector,variant['code'])+' OR '+predicate(field,restriction)+')')
        elif 'domain' in field:result.append(predicate(field,field['domain']))
    operators={'eq':'=','ne':'<>','lt':'<','le':'<=','gt':'>','ge':'>='}
    for rule in value['rules']:
        left='"'+rule['field']+'"'
        if rule['kind']=='required':result.append(left+' IS NOT NULL')
        else:
            operand=rule['operand']
            right='"'+operand['field']+'"' if 'field' in operand else literal(fields[rule['field']],operand['literal'])
            # Explicit null comparison is false, not SQL CHECK's unknown success.
            result.append('('+left+' '+operators[rule['operator']]+' '+right+') IS TRUE')
    return result
