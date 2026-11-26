import ast, pathlib, unittest
HERE=pathlib.Path(__file__).resolve().parent
RUNTIME=HERE/'payload/backend/app/reception_v3/runtime.py'
class Tests(unittest.TestCase):
 def test_compact_upgrade_and_repeat(self):
  root=ast.parse(RUNTIME.read_text(encoding='utf8'))
  fn=next(n for n in ast.walk(root) if isinstance(n,ast.FunctionDef) and n.name=='load')
  class Registry:
   def __init__(self):self.calls=[]
   def load(self,name,compact=False):
    self.calls.append((name,compact));return {'digest':name+str(compact),'instructions':'facts'}
  r=Registry();scope={'registry':r,'loaded':{},'loaded_views':{}}
  exec(compile(ast.Module(body=[fn],type_ignores=[]),'loader','exec'),scope)
  load=scope['load'];load('peach',compact=True)
  self.assertTrue(load('peach',compact=True)['already_loaded'])
  self.assertNotIn('already_loaded',load('peach'))
  self.assertTrue(load('peach')['already_loaded'])
  self.assertEqual(r.calls,[('peach',True),('peach',False)])
 def test_deterministic_retry(self):
  root=ast.parse((RUNTIME.parent/'service.py').read_text(encoding='utf8'))
  expr=next(n.value for n in ast.walk(root) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='maximum_attempts' for t in n.targets))
  code=compile(ast.Expression(expr),'retry','eval')
  self.assertEqual(eval(code,{'error':'v3_context_too_large'}),1)
  self.assertEqual(eval(code,{'error':'v3_model_timeout'}),3)
 def test_python_sources_compile(self):
  for p in (HERE/'payload').rglob('*.py'):compile(p.read_text(encoding='utf8'),str(p),'exec')
if __name__=='__main__':unittest.main()
