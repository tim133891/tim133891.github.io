#!/usr/bin/env python3
"""Dependency-free RHEL server management MCP, stdio JSON-RPC."""
import json, os, pathlib, signal, subprocess, sys, tempfile, threading, time, uuid, re, fnmatch, pty, select

JOBS={}
SESSIONS={}
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
TOOLS.extend([
 {'name':'read_multiple_files','description':"Read up to 20 text files",'inputSchema':O({"paths":{'type':'array','items':S('Absolute path'),'maxItems':20}},["paths"])},
 {'name':'edit_block','description':"Replace a unique exact block in text file",'inputSchema':O({"path":S('path'),"old_text":S('old_text'),"new_text":S('new_text')},["path","old_text","new_text"])},
 {'name':'get_file_info','description':"Get file size, mode, owner, and dates",'inputSchema':O({"path":S('path')},["path"])},
 {'name':'create_directory','description':"Create directories recursively",'inputSchema':O({"path":S('path')},["path"])},
 {'name':'move_file','description':"Rename or move without overwriting",'inputSchema':O({"source":S('source'),"destination":S('destination')},["source","destination"])},
 {'name':'search_files','description':"Bounded recursive filename and text search",'inputSchema':O({"path":S('path'),"name_glob":S('name_glob'),"contains_text":S('contains_text'),"max_results":I('Number')},["path"])},
 {'name':'list_processes','description':"Show Linux process list",'inputSchema':O({},[])},
 {'name':'kill_process','description':"Signal a process",'inputSchema':O({"pid":I('Number'),"signal":S('signal')},["pid"])},
 {'name':'list_ports','description':"Show listening TCP UDP sockets",'inputSchema':O({},[])},
 {'name':'service_status','description':"Inspect systemd service",'inputSchema':O({"service":S('service')},["service"])},
 {'name':'service_action','description':"Start stop restart reload enable disable systemd unit; requires sudo privileges",'inputSchema':O({"service":S('service'),"action":S('action')},["service","action"])},
 {'name':'list_jobs','description':"Show background jobs",'inputSchema':O({},[])},
 {'name':'start_session','description':"Create interactive Bash PTY session",'inputSchema':O({"cwd":S('cwd')},[])},
 {'name':'session_input','description':"Type text in PTY and get output",'inputSchema':O({"session_id":S('session_id'),"input":S('input'),"wait_ms":I('Number')},["session_id","input"])},
 {'name':'session_output','description':"Get interactive PTY output",'inputSchema':O({"session_id":S('session_id'),"wait_ms":I('Number')},["session_id"])},
 {'name':'list_sessions','description':"List interactive Bash sessions",'inputSchema':O({},[])},
 {'name':'stop_session','description':"Stop interactive session",'inputSchema':O({"session_id":S('session_id')},["session_id"])},
 {'name':'read_pdf','description':"Extract PDF text via pdftotext if installed",'inputSchema':O({"path":S('path'),"pages":I('Number')},["path"])},
 {'name':'write_pdf','description':"Create simple text PDF via reportlab if installed",'inputSchema':O({"path":S('path'),"text":S('text'),"overwrite":{'type':'boolean'}},["path","text"])},
 {'name':'read_url','description':"Fetch an HTTPS URL using curl",'inputSchema':O({"url":S('url')},["url"])},
])

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

def many(a):
    paths=a['paths']
    if len(paths)>20: raise ValueError('Too many paths')
    out=[]
    for name in paths:
        try: out.append(read({'path':name,'max_bytes':32768}))
        except Exception as e: out.append({'path':name,'error':str(e)})
    return {'files':out}
def edit(a):
    p=path(a['path']); content=p.read_text(encoding='utf-8')
    old=a['old_text']
    if not old or content.count(old)!=1: raise ValueError('old_text must match exactly once')
    return write({'path':str(p),'content':content.replace(old,a['new_text'],1),'overwrite':True})
def stat(a):
    p=path(a['path']); v=p.lstat()
    return {'path':str(p),'bytes':v.st_size,'mode':oct(v.st_mode&0o7777),'uid':v.st_uid,'gid':v.st_gid,'mtime':v.st_mtime,'directory':p.is_dir()}
def mkdir(a):
    p=path(a['path']); p.mkdir(parents=True,exist_ok=True)
    return {'path':str(p)}
def move(a):
    src=path(a['source']); dst=path(a['destination'])
    if dst.exists() or dst.is_symlink(): raise ValueError('Destination already exists')
    src.rename(dst)
    return {'from':str(src),'to':str(dst)}
def search(a):
    base=path(a['path']); pattern=a.get('name_glob') or '*'; needle=a.get('contains_text')
    results=[]; n=0; start=time.monotonic(); limit=min(100,max(1,int(a.get('max_results',50))))
    for root,dirs,files in os.walk(base):
        dirs[:]=[d for d in dirs if d not in ('.git','node_modules','.cache')]
        for fn in files:
            n+=1
            if n>12000 or time.monotonic()-start>10: return {'matches':results,'scanned':n,'truncated':True}
            if not fnmatch.fnmatch(fn,pattern): continue
            p=pathlib.Path(root)/fn
            if needle:
                try:
                    if p.stat().st_size>1000000 or needle not in p.read_text(encoding='utf-8'): continue
                except (OSError,UnicodeError): continue
            results.append(str(p))
            if len(results)>=limit: return {'matches':results,'scanned':n,'truncated':True}
    return {'matches':results,'scanned':n,'truncated':False}
def processes(a):
    return run({'command':'ps -eo pid,ppid,user,%cpu,%mem,stat,etimes,args --sort=-%cpu | head -80','timeout_seconds':10})
def kill(a):
    pid=int(a['pid'])
    if pid<=1 or pid==os.getpid(): raise ValueError('Invalid process ID')
    sig=signal.SIGKILL if a.get('signal','TERM')=='KILL' else signal.SIGTERM
    os.kill(pid,sig)
    return {'pid':pid,'signal':sig.name}
def ports(a): return run({'command':'ss -tulpen','timeout_seconds':10})
def unit(s):
    if not re.fullmatch(r'[a-zA-Z0-9_.@:-]+',s): raise ValueError('Invalid service')
    return s
def service(a):
    return run({'command':'systemctl --no-pager status '+unit(a['service'])+' || true','timeout_seconds':10})
def serviceact(a):
    action=a['action']
    if action not in ('start','stop','restart','reload','enable','disable'): raise ValueError('Bad action')
    return run({'command':'sudo -n systemctl '+action+' '+unit(a['service']),'timeout_seconds':30})
def jobs(a):
    with LOCK: items=list(JOBS.items())
    return {'jobs':[{'job_id':k,'pid':v[0].pid,'running':v[0].poll() is None} for k,v in items]}
def get_session(id):
    with LOCK: v=SESSIONS.get(id)
    if not v: raise ValueError('Unknown session')
    return v
def start_session(a):
    master,slave=pty.openpty()
    try:
        p=subprocess.Popen(['/bin/bash','--noprofile','--norc','-i'],stdin=slave,stdout=slave,stderr=slave,env={**env(),'TERM':'xterm'},cwd=str(path(a['cwd'])) if a.get('cwd') else None,start_new_session=True)
    finally: os.close(slave)
    os.set_blocking(master,False)
    id=uuid.uuid4().hex[:12]
    with LOCK: SESSIONS[id]=(p,master)
    return {'session_id':id,'pid':p.pid}
def read_session(a):
    id=a['session_id']; p,fd=get_session(id)
    wait=min(max(int(a.get('wait_ms',200)),0),1500)/1000
    data=bytearray(); until=time.monotonic()+wait
    while len(data)<48000:
        ready,_,_=select.select([fd],[],[],max(0,until-time.monotonic()))
        if not ready: break
        try: new=os.read(fd,4096)
        except OSError: break
        if not new: break
        data.extend(new)
        if time.monotonic()>=until: break
    return {'session_id':id,'running':p.poll() is None,'output':data.decode('utf-8','replace')}
def send_session(a):
    p,fd=get_session(a['session_id'])
    text=a['input']
    if len(text)>10000: raise ValueError('Input too long')
    os.write(fd,text.encode())
    return read_session(a)
def sessions(a):
    with LOCK: items=list(SESSIONS.items())
    return {'sessions':[{'id':i,'pid':v[0].pid,'running':v[0].poll() is None} for i,v in items]}
def stop_session(a):
    id=a['session_id']; p,fd=get_session(id)
    if p.poll() is None: os.killpg(p.pid,signal.SIGTERM)
    os.close(fd)
    with LOCK: SESSIONS.pop(id,None)
    return {'stopped':id}
def readpdf(a):
    p=str(path(a['path'])); pages=max(1,min(30,int(a.get('pages',15))))
    return run({'command':'pdftotext -f 1 -l '+str(pages)+' -layout -- '+__import__('shlex').quote(p)+' -','timeout_seconds':30})
def writepdf(a):
    p=path(a['path'])
    if p.exists() and not a.get('overwrite',False): raise FileExistsError(str(p))
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    c=canvas.Canvas(str(p),pagesize=letter); y=745
    for line in a['text'].splitlines():
        while len(line)>100:
            c.drawString(40,y,line[:100]); line=line[100:]; y-=13
            if y<50: c.showPage(); y=745
        c.drawString(40,y,line); y-=13
        if y<50: c.showPage(); y=745
    c.save()
    return {'path':str(p),'bytes':p.stat().st_size}
def readurl(a):
    url=a['url']
    if not url.startswith('https://'): raise ValueError('HTTPS required')
    return run({'command':'curl -fLsS --max-time 15 --max-filesize 131072 '+__import__('shlex').quote(url),'timeout_seconds':20})
DISPATCH.update({'read_multiple_files':many,'edit_block':edit,'get_file_info':stat,'create_directory':mkdir,'move_file':move,'search_files':search,'list_processes':processes,'kill_process':kill,'list_ports':ports,'service_status':service,'service_action':serviceact,'list_jobs':jobs,'start_session':start_session,'session_input':send_session,'session_output':read_session,'list_sessions':sessions,'stop_session':stop_session,'read_pdf':readpdf,'write_pdf':writepdf,'read_url':readurl})

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
