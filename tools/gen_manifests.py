import ast, json, re, sys, textwrap
from pathlib import Path
ROOT = Path('.').resolve()
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'vendor'))

# ---------------- REST ----------------
src = (ROOT / 'bbu/api.py').read_text()
tree = ast.parse(src)
funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
pat = re.compile(r'\(\?P<(.+?)>')

def template(p):
    out=[]; i=0
    while True:
        m=pat.search(p,i)
        if not m:
            out.append(p[i:]); break
        out.append(p[i:m.start()]); name=m.group(1); j=m.end(); depth=1
        while j<len(p) and depth:
            if p[j]=='(': depth+=1
            elif p[j]==')': depth-=1
            j+=1
        out.append('{'+name+'}'); i=j
    return ''.join(out)

def calls(node, name):
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == name:
            yield n

def const_str(n):
    try:
        v=ast.literal_eval(n)
        return v if isinstance(v, str) else None
    except Exception:
        return None

def handler_params(node):
    body_vars=set(); query=set(); body=set(); form=set()
    for n in ast.walk(node):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            f=n.value.func
            if isinstance(f, ast.Attribute) and f.attr in ('json','form','multipart'):
                for t in n.targets:
                    if isinstance(t, ast.Name): body_vars.add(t.id)
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr=='get' and n.args:
            key=const_str(n.args[0])
            if not key: continue
            f=n.func.value
            if isinstance(f, ast.Attribute) and f.attr=='query':
                query.add(key)
            elif isinstance(f, ast.Name) and f.id in body_vars:
                body.add(key)
        if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id in body_vars:
            key=const_str(n.slice)
            if key: body.add(key)
    return sorted(query), sorted(body)

def called_names(node):
    return {n.func.id for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}

def handler_auth(node):
    names=called_names(node)
    if names & {'require_admin','_require_admin','_admin'}:
        return 'admin'
    if names & {'_auth','require_user','require_auth'}:
        return 'user'
    return None

# 沿调用图上卷鉴权等级，处理 h_* -> _task_ctx -> _auth 这类间接调用
rank={'public':0,'user':1,'admin':2}
# 先给所有函数一个直接等级
direct={name:handler_auth(fn) for name,fn in funcs.items()}
levels={name:(direct.get(name) or 'public') for name in funcs}
for _ in range(6):
    for name,fn in funcs.items():
        cur=levels[name]
        for callee in called_names(fn):
            other=levels.get(callee)
            if other and rank[other]>rank[cur]: cur=other
        levels[name]=cur

def handler_errors(node):
    out=set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id=='HTTPError' and n.args:
            try:
                code=int(ast.literal_eval(n.args[0]))
            except Exception:
                continue
            out.add(code)
    return sorted(out)

def is_binary(path):
    return any(x in path for x in ('/download','/export','/report','/manifest')) or path.endswith('/package')

calls_list=[]
for n in ast.walk(tree):
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr=='add' and len(n.args)>=2:
        m=const_str(n.args[0]); p=const_str(n.args[1])
        if (m and p and (p.startswith('/api') or p=='/mcp')):
            calls_list.append((n.lineno,n,m,p))
calls_list.sort(key=lambda x:x[0])
routes=[]
idx=0
for line,n,m,p in calls_list:
        idx+=1
        hname=''
        if len(n.args)>2:
            hname=const_str(n.args[2]) or (n.args[2].id if isinstance(n.args[2], ast.Name) else '')
        f=funcs.get(hname)
        t=template(p)
        path_params=[]
        for name in re.findall(r'\{([^}]+)\}', t):
            typ='integer' if name in ('user_id','task_id') and re.search(r'\[0-9\]', p) else 'string'
            path_params.append({'name':name,'in':'path','type':typ,'required':True})
        q,b=handler_params(f) if f else ([],[])
        params=path_params+[{'in':'query','name':x,'required':False} for x in q]+[{'in':'body','name':x,'required':False} for x in b]
        routes.append({
            'id': f'route-{idx:03d}', 'method': m, 'path': t, 'regex': p,
            'handler': hname or 'inline', 'auth': levels.get(hname,'public') if f else 'public',
            'mutating': m not in ('GET','HEAD'),
            'binary': is_binary(t),
            'params': params,
            'expected_errors': handler_errors(f) if f else [],
            'case_ids': [f'rest-{idx:03d}-happy', f'rest-{idx:03d}-auth'],
            'notes': '由 tools/gen_manifests.py 从 bbu/api.py 路由与处理函数 AST 生成',
        })
manifest_routes={'schema':1,'generated_by':'tools/gen_manifests.py','total':len(routes),'routes':routes}
(ROOT/'tests/manifests/rest_routes.json').write_text(json.dumps(manifest_routes, ensure_ascii=False, indent=2)+'\n')

# ---------------- UI ----------------
js_files=sorted((ROOT/'web').rglob('*.js'))+sorted((ROOT/'web').rglob('*.html'))
act_re=re.compile(r'data-act=["\']([^"\']+)["\']')
actions={}
button_count=0; page_button_count=0; modal_count=0; forms=[]
for f in js_files:
    s=f.read_text(errors='ignore')
    rel=str(f.relative_to(ROOT))
    button_count += len(re.findall(r'<button\b', s))
    if f.name.startswith('page_') or f.name=='app.js':
        page_button_count += len(re.findall(r'<button\b', s))
    modal_count += len(re.findall(r'\bmodal\(', s))
    forms += [(i, rel) for i in re.findall(r'<form\b[^>]*\bid=["\']([^"\']+)["\']', s)]
    for m in act_re.finditer(s):
        a=m.group(1)
        item=actions.setdefault(a, {'action':a,'instances':0,'files':[]})
        item['instances']+=1
        if rel not in item['files']: item['files'].append(rel)
ui={
 'schema':1,'generated_by':'tools/gen_manifests.py',
 'counts':{'distinct_actions':len(actions),'action_instances':sum(x['instances'] for x in actions.values()),
           'buttons_all':button_count,'buttons_page':page_button_count,'modal_calls':modal_count,'forms':len(forms)},
 'actions':sorted(actions.values(), key=lambda x:x['action']),
 'forms':sorted({x[0] for x in forms}),
 'pages':sorted(p.name for p in (ROOT/'web/assets/js').glob('page_*.js')),
}
(ROOT/'tests/manifests/ui_actions.json').write_text(json.dumps(ui, ensure_ascii=False, indent=2)+'\n')

# ---------------- tools + mcp ----------------
from bbu.assistant_tools import ToolRegistry
reg=ToolRegistry(None, None, None)
tools=[]
for spec in reg._tools.values():
    tools.append({'name':spec.name,'title':spec.title,'description':spec.description,
                  'scope':spec.scope,'risk':spec.risk,'mcp':spec.mcp,'schema':spec.schema})
mcp_src=(ROOT/'bbu/mcp_server.py').read_text()
mcp_methods=['initialize','ping','logging/setLevel','tools/list','tools/call','resources/list','resources/read','prompts/list','prompts/get','roots/list','sampling/createMessage','completion/complete','notifications/initialized','notifications/cancelled']
(ROOT/'tests/manifests/tool_specs.json').write_text(json.dumps(
 {'schema':1,'generated_by':'tools/gen_manifests.py','assistant_tools':tools,'mcp_methods':mcp_methods,
  'counts':{'assistant_tools':len(tools),'mcp_methods':len(mcp_methods)}}, ensure_ascii=False, indent=2)+'\n')

# ---------------- CLI ----------------
scripts=[]
for f in sorted((ROOT/'meta/bbu-fault-skill-builder/scripts').glob('*.py')):
    scripts.append({'path':str(f.relative_to(ROOT)),'kind':'meta-script'})
for f in sorted((ROOT/'tools').glob('*.py')):
    if f.name=='gen_manifests.py': continue
    scripts.append({'path':str(f.relative_to(ROOT)),'kind':'platform-tool'})
for f in sorted((ROOT/'deploy').glob('*.sh')):
    scripts.append({'path':str(f.relative_to(ROOT)),'kind':'deploy-shell'})
(ROOT/'tests/manifests/cli_options.json').write_text(json.dumps(
 {'schema':1,'generated_by':'tools/gen_manifests.py','count':len(scripts),'scripts':scripts}, ensure_ascii=False, indent=2)+'\n')
print('routes',len(routes),'actions',len(actions),sum(x['instances'] for x in actions.values()),'buttons',button_count,page_button_count,'modals',modal_count,'forms',len(ui['forms']),'tools',len(tools),'mcp',len(mcp_methods),'cli',len(scripts))
