#!/usr/bin/env python3
"""Dependency-free RHEL server management MCP, stdio JSON-RPC."""
import json, os, pathlib, signal, subprocess, sys, tempfile, threading, time, uuid

JOBS={}
LOCK=threading.Lock()
KEYS={'CONTROL_PLANE_API_KEY','CONTROL_PLANE_TUNNEL_ID','OPENAI_API_KEY','OPENAI_ADMIN_KEY'}
def env():
    return {k:v for k,v in os.environ.items() if k not in KEYS and not k.endswith(('_TOKEN','_SECRET'))}
def S(h): return {'type':'string','description':h}
def I(h): return {'type':'integer','description':h}
def O(p,required=()): return {'type':'object','properties':p,'required':list(required),'additionalProperties':False}
TOOLS=[
 {'name':'run_command','description':'Run a Bash command on RHEL as the SSH user, not root; returns output and exit code. Max 120 sec. sudo requires separately configured noninteractive privileges.',
  'inputSchema':O({'command':S('Bash command'),'timeout_seconds':I('Timeout 1-120 seconds; default 30'),'cwd':S('Optional absolute working directory')},['command'])},
 {'name':'read_file','description':'Read a text file accessible to the SSH user. Do not read secrets unless specifically requested.',
  'inputSchema':O({'path':S('Absolute file path'),'offset':I('Byte offset'),'max_bytes':I('Maximum 131072 bytes')},['path'])},
 {'name':'write_file','description':'Create or overwrite a text file as the SSH user. Private 0600 permissions on new files.',
  'inputSchema':O({'path':S('Absolute file path'),'content':S('Text to write'),'overwrite':{'type':'boolean','default':False}},['path','content'])},
 {'name':'list_directory','description':'List directories and file sizes on RHEL.',
  'inputSchema':O({'path':S('Absolute directory path'),'limit':I('Maximum 300 entries')},['path'])},
 {'name':'system_info','description':'Inspect hostname, kernel, CPU, NUMA, RAM, and disk layout.',
  'inputSchema':O({})},
 {'name':'start_job','description':'Start a long-running command as a background job. Query with job_status; jobs are tracked until MCP process restarts.',
  'inputSchema':O({'command':S('Bash command'),'cwd':S('Optional absolute working directory')},['command'])},
 {'name':'job_status','description':'Read status and log tail of a running or completed background job.',
  'inputSchema':O({'job_id':S('Job ID'),'tail_bytes':I('Max 65536 log bytes')},['job_id'])},
 {'name':'stop_job','description':'Terminate a job started by start_job.',
  'inputSchema':O({'job_id':S('Job ID')},['job_id'])}
]
def path(x):
    p=pathlib.Path(x).expanduser()
    if not p.is_absolute(): raise ValueError('Absolute path required')
    return p
def cmd(x):
    if not isinstance(x,str) or not x.strip() or len(x)>12000: raise ValueError('Command must be 1-12000 characters')
    return x
def run(a):
    timeout=min(max(int(a.get('timeout_seconds',30)),1),120)
    cwd=str(path(a['cwd'])) if a.get('cwd') else None
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        p=subprocess.Popen(['/bin/bash','-lc',cmd(a['command'])],cwd=cwd,env=env(),stdout=out,stderr=err,stdin=subprocess.DEVNULL,start_new_session=True)
        expired=False
        try: p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            expired=True
            try: os.killpg(p.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            p.wait()
        out.seek(0); err.seek(0)
        return {'exit_code':None if expired else p.returncode,'timed_out':expired,
                'stdout':out.read(75000).decode('utf-8','replace'),
                'stderr':err.read(75000).decode('utf-8','replace')}
def read(a):
    p=path(a['path']); off=max(0,int(a.get('offset',0))); n=min(max(1,int(a.get('max_bytes',65536))),131072)
    with p.open('rb') as f:
        f.seek(off); data=f.read(n); more=bool(f.read(1))
    return {'path':str(p),'offset':off,'bytes_read':len(data),'more':more,'text':data.decode('utf-8','replace')}
def write(a):
    p=path(a['path']); value=a['content']
    if not isinstance(value,str) or len(value.encode())>1048576: raise ValueError('Content must be UTF-8 text up to 1 MiB')
    p.parent.mkdir(parents=True,exist_ok=True)
    if a.get('overwrite'):
        mode=(p.stat().st_mode & 0o777) if p.exists() else 0o600
        fd,tmp=tempfile.mkstemp(prefix='.rhel-mcp-',dir=str(p.parent))
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f: f.write(value)
            os.chmod(tmp,mode); os.replace(tmp,p)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    else:
        with p.open('x',encoding='utf-8') as f: f.write(value)
        os.chmod(p,0o600)
    return {'path':str(p),'bytes_written':len(value.encode())}
def listing(a):
    p=path(a['path']); entries=sorted(p.iterdir(),key=lambda v:v.name)
    result=[]
    for item in entries[:min(max(1,int(a.get('limit',100))),300)]:
        stat=item.lstat()
        result.append({'name':item.name,'type':'directory' if item.is_dir() else 'symlink' if item.is_symlink() else 'file','size':stat.st_size,'mode':oct(stat.st_mode & 0o777)})
    return {'path':str(p),'total':len(entries),'entries':result}
def job(a):
    with LOCK: v=JOBS.get(a['job_id'])
    if not v: raise ValueError('Unknown job; jobs are tracked only until server restart')
    return v
def start(a):
    j=uuid.uuid4().hex[:12]
    d=pathlib.Path.home()/'.local/state/rhel-remote-mcp/jobs'
    d.mkdir(parents=True,exist_ok=True,mode=0o700)
    log=d/(j+'.log'); cwd=str(path(a['cwd'])) if a.get('cwd') else None
    with log.open('wb') as f:
        p=subprocess.Popen(['/bin/bash','-lc',cmd(a['command'])],cwd=cwd,env=env(),stdin=subprocess.DEVNULL,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
    with LOCK: JOBS[j]=(p,log,time.time())
    return {'job_id':j,'pid':p.pid,'status':'running'}
def status(a):
    p,log,started=job(a)
    n=min(max(1,int(a.get('tail_bytes',16000))),65536)
    with log.open('rb') as f:
        f.seek(0,os.SEEK_END); f.seek(max(0,f.tell()-n)); txt=f.read().decode('utf-8','replace')
    code=p.poll()
    return {'job_id':a['job_id'],'pid':p.pid,'status':'running' if code is None else 'exited','exit_code':code,'elapsed_seconds':round(time.time()-started,1),'log_tail':txt}
def stop(a):
    p,_,_=job(a); active=p.poll() is None
    if active: os.killpg(p.pid,signal.SIGTERM)
    return {'job_id':a['job_id'],'signal':'SIGTERM' if active else 'already_exited'}
def info(a):
    return run({'command':"echo HOST; hostname; echo KERNEL; uname -r; echo CPU; lscpu | head -35; echo NUMA; if command -v numactl >/dev/null; then numactl --hardware; else echo 'numactl not installed'; fi; echo MEMORY; free -h; echo DISKS; lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS",'timeout_seconds':20})
DISPATCH={'run_command':run,'read_file':read,'write_file':write,'list_directory':listing,'system_info':info,'start_job':start,'job_status':status,'stop_job':stop}
def handle(m):
    method=m.get('method'); p=m.get('params') or {}
    if method=='initialize':
        v=p.get('protocolVersion')
        if v not in ('2024-11-05','2025-03-26','2025-06-18','2025-11-25'): v='2025-06-18'
        return {'protocolVersion':v,'capabilities':{'tools':{'listChanged':False}},'serverInfo':{'name':'rhel-remote-manager','version':'1.0.0'}}
    if method=='ping': return {}
    if method=='tools/list': return {'tools':TOOLS}
    if method=='tools/call':
        name=p.get('name')
        if name not in DISPATCH: return {'content':[{'type':'text','text':'Unknown tool'}],'isError':True}
        try:
            result=DISPATCH[name](p.get('arguments') or {})
            return {'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}],'isError':False}
        except Exception as e:
            return {'content':[{'type':'text','text':type(e).__name__+': '+str(e)}],'isError':True}
    return None
for line in sys.stdin:
    try:
        m=json.loads(line)
        if 'id' not in m: continue
        result=handle(m)
        response={'jsonrpc':'2.0','id':m['id']}
        if result is None: response['error']={'code':-32601,'message':'Method not found'}
        else: response['result']=result
    except Exception as e:
        response={'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':str(e)}}
    print(json.dumps(response,ensure_ascii=False,separators=(',',':')),flush=True)
