import ast
from pathlib import Path
from types import SimpleNamespace
import pytest

def loader():
    source=Path(__file__).parent/'payload/backend/app/reception_v3/runtime.py'
    tree=ast.parse(source.read_text(encoding='utf8'))
    run=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='run_agent')
    node=next(n for n in run.body if isinstance(n,ast.FunctionDef) and n.name=='load')
    calls=[]
    def read(name):
        calls.append(name)
        if name=='missing': raise KeyError(name)
        return {'digest':name+'-hash','instructions':'full-'+name}
    scope={'registry':SimpleNamespace(load=read),'loaded':{}}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),scope)
    return scope['load'],calls

def test_repeated_skill_retains_reference_without_repeating_full_content():
    load,calls=loader()
    first=load('peach');second=load('peach')
    assert first['instructions']=='full-peach'
    assert second['digest']==first['digest'] and second['already_loaded']
    assert 'instructions' not in second
    assert calls==['peach']

def test_different_skill_still_loads_full_content():
    load,calls=loader()
    load('ordinary')
    assert load('peach')['instructions']=='full-peach'
    assert calls==['ordinary','peach']

def test_failed_read_is_not_cached():
    load,calls=loader()
    for _ in range(2):
        with pytest.raises(KeyError): load('missing')
    assert calls==['missing','missing']

@pytest.mark.parametrize('intent,target,expected',[
    ('select','peach',['peach']),
    ('compare','peach',['ordinary','peach']),
    ('select','missing',['ordinary','peach']),
])
def test_preload_reduction_only_for_valid_explicit_selection(intent,target,expected):
    source=Path(__file__).parent/'payload/backend/app/reception_v3/runtime.py'
    tree=ast.parse(source.read_text(encoding='utf8'))
    run=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='run_agent')
    node=next(n for n in run.body if isinstance(n,ast.If)
              and "understanding.get('intent') == 'select'" in ast.unparse(n.test))
    context={'skills':{'routes':{'ordinary':{},'peach':{}}},'route_variant':'ordinary'}
    scope={'understanding':{'intent':intent,'selected_route':target},'context':context,'routes':['ordinary','peach']}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),scope)
    assert scope['routes']==expected
    assert context['route_variant']=='ordinary'

def test_visual_projection_keeps_both_comparison_routes_without_mutating_source():
    source=Path(__file__).parent/'payload/backend/app/reception_v3/runtime.py'
    tree=ast.parse(source.read_text(encoding='utf8'))
    run=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='run_agent')
    index=next(i for i,n in enumerate(run.body) if isinstance(n,ast.Assign)
               and any(isinstance(t,ast.Name) and t.id=='visual_routes' for t in n.targets))
    original={'ordinary':[1],'peach':[2],'yunnan':[3]}
    scope={'routes':['ordinary'],'understanding':{'discussed_routes':['peach']},
           'current':{'itinerary_previews':original,'answer_visuals':original}}
    exec(compile(ast.Module(body=run.body[index:index+2],type_ignores=[]),str(source),'exec'),scope)
    assert scope['current']['itinerary_previews']=={'ordinary':[1],'peach':[2]}
    assert scope['current']['answer_visuals']=={'ordinary':[1],'peach':[2]}
    assert original=={'ordinary':[1],'peach':[2],'yunnan':[3]}
