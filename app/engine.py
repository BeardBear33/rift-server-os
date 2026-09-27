import contextlib, hashlib, json, os, re, secrets, shutil, socket, sqlite3, subprocess, tarfile, threading, time, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from pathlib import Path
BASE=Path(os.getenv('RIFT_BASE','/srv/rift')); RUN=Path(os.getenv('RIFT_RUN','/run/rift')); DATA=BASE/'servers'; ARCH=BASE/'backups'; DB=BASE/'panel.db'
ID=re.compile(r'^[a-f0-9]{16}$'); VER=re.compile(r'^(1\.\d{2}(?:\.\d{1,2})?|\d{2}\.\d+(?:\.\d+)?)$'); NAME=re.compile(r'^[a-z][a-z0-9_]{2,31}$'); KINDS=('neoforge','paper','spigot','craftbukkit','vanilla')
UA='RiftServerOS/1.0 (https://github.com/openai)'
LOCK=threading.RLock(); ACTIVE=set(); _SAMPLE={}
def db():
    BASE.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(DB,timeout=30,check_same_thread=False);DB.chmod(0o600);c.row_factory=sqlite3.Row;c.execute('PRAGMA journal_mode=WAL');return c
def init():
    DATA.mkdir(parents=True,exist_ok=True);ARCH.mkdir(parents=True,exist_ok=True)
    with db() as c:c.executescript('''CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY,salt TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,name TEXT NOT NULL,csrf TEXT NOT NULL,expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS servers(id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,version TEXT NOT NULL,loader TEXT NOT NULL DEFAULT '',port INTEGER UNIQUE NOT NULL,ram INTEGER NOT NULL,java INTEGER NOT NULL,state TEXT NOT NULL,autostart INTEGER NOT NULL DEFAULT 0,restart_hours INTEGER NOT NULL DEFAULT 0,next_restart INTEGER NOT NULL DEFAULT 0,created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,server TEXT,kind TEXT,state TEXT,log TEXT NOT NULL DEFAULT '',created INTEGER,ended INTEGER);
CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY,server TEXT NOT NULL,name TEXT NOT NULL,paths TEXT NOT NULL,destination TEXT NOT NULL,hours INTEGER NOT NULL,keep INTEGER NOT NULL,next_run INTEGER NOT NULL,offline INTEGER NOT NULL);''')
def get(sid):
    if not ID.fullmatch(str(sid)):raise ValueError('Neplatné ID serveru')
    with db() as c:s=c.execute('SELECT * FROM servers WHERE id=?',(sid,)).fetchone()
    if s is None:raise ValueError('Server neexistuje')
    return dict(s)
def running(sid):return (RUN/(sid+'.sock')).exists()
def servers():
    with db() as c:rows=[dict(x) for x in c.execute('SELECT * FROM servers ORDER BY created DESC')]
    for row in rows:row['running']=running(row['id'])
    return rows
def socket_call(path,obj,timeout=90):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(timeout);sock.connect(str(path));sock.sendall((json.dumps(obj)+'\n').encode());raw=bytearray()
        while b'\n' not in raw and len(raw)<65536:
            part=sock.recv(4096)
            if not part:break
            raw.extend(part)
    result=json.loads(raw.split(b'\n')[0]);
    if not result.get('ok'):raise RuntimeError(result.get('error','Služba selhala'))
    return result
def privileged(action,**kw):return socket_call(RUN/'root.sock',{'action':action,**kw},130)
def command(sid,value):
    get(sid);value=str(value).lstrip('/').strip()
    if not value or len(value)>400 or '\n' in value or '\r' in value:raise ValueError('Neplatný příkaz')
    return socket_call(RUN/(sid+'.sock'),{'command':value},5)
def stop(sid):
    get(sid);was=running(sid)
    if was:
        with contextlib.suppress(Exception):command(sid,'stop')
        until=time.monotonic()+75
        while running(sid) and time.monotonic()<until:time.sleep(.25)
    privileged('service',name='rift-mc@'+sid+'.service',verb='stop');return was
def start(sid):
    if get(sid)['state']!='ready':raise ValueError('Instalace ještě není dokončená')
    return privileged('service',name='rift-mc@'+sid+'.service',verb='start')
def path(sid,rel):
    root=(DATA/get(sid)['id']).resolve(strict=True);p=(root/str(rel).lstrip('/')).resolve()
    if p!=root and root not in p.parents:raise ValueError('Cesta mimo server')
    return p
def job(kind,sid,fn):
    jid=secrets.token_hex(8)
    with db() as c:c.execute('INSERT INTO jobs(id,server,kind,state,created) VALUES(?,?,?,?,?)',(jid,sid,kind,'running',int(time.time())))
    def runner():
        try:fn(jid);update(jid,'Dokončeno','done')
        except Exception as e:update(jid,'Chyba: '+str(e),'failed')
    threading.Thread(target=runner,daemon=True).start();return jid
def update(jid,text,state=None):
    with db() as c:c.execute('UPDATE jobs SET log=substr(log || ?, -16000),state=coalesce(?,state),ended=CASE WHEN ? IS NULL THEN ended ELSE ? END WHERE id=?',('\n'+str(text),state,state,int(time.time()),jid))
def fetch(url,dest=None,limit=1024*1024*1024):
    domains=('neoforged.net','papermc.io','mojang.com','minecraft.net','spigotmc.org')
    def validate(u):
        p=urllib.parse.urlsplit(u);h=(p.hostname or '').lower()
        if p.scheme!='https' or not any(h==d or h.endswith('.'+d) for d in domains):raise ValueError('Nepovolený server stahování')
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,new):validate(new);return super().redirect_request(req,fp,code,msg,headers,new)
    validate(url)
    with urllib.request.build_opener(Redirect()).open(urllib.request.Request(url,headers={'User-Agent':UA}),timeout=50) as r:
        if dest is None:
            value=r.read(min(limit+1,5*1024*1024))
            if len(value)>limit:raise ValueError('Odpověď přesáhla limit')
            return value
        dest=Path(dest);part=Path(str(dest)+'.part');n=0
        try:
            with part.open('wb') as f:
                while block:=r.read(262144):
                    n+=len(block)
                    if n>limit:raise ValueError('Stažený soubor překročil limit')
                    f.write(block)
            part.replace(dest)
        finally:part.unlink(missing_ok=True)
def json_url(url):return json.loads(fetch(url,limit=4*1024*1024))
def manifest():return json_url('https://piston-meta.mojang.com/mc/game/version_manifest_v2.json')
def version_info(version):
    if not VER.fullmatch(str(version)):raise ValueError('Neplatná verze')
    matches=[v for v in manifest()['versions'] if v['id']==version and v['type']=='release']
    if not matches:raise ValueError('Verze není v oficiálním seznamu Minecraftu')
    detail=json_url(matches[0]['url']);java=int(detail.get('javaVersion',{}).get('majorVersion',21))
    if java not in (21,25):raise ValueError('Zvolená verze vyžaduje Javu '+str(java)+'; k dispozici je Java 21/25')
    return java,detail
def available(kind):
    if kind not in KINDS:raise ValueError('Neznámý typ serveru')
    if kind=='paper':return [v for group in json_url('https://fill.papermc.io/v3/projects/paper')['versions'].values() for v in group if VER.fullmatch(v)][:150]
    return [v['id'] for v in manifest()['versions'] if v['type']=='release' and VER.fullmatch(v['id']) and (v['id'].startswith(('1.21.','1.22.','26.')) or v['id']=='1.21')][:150]
def choose_neo(version):
    parts=version.split('.')
    if parts[0]!='1' or len(parts)<2:raise ValueError('NeoForge pro tuto verzi není k dispozici')
    prefix=parts[1]+'.'+(parts[2] if len(parts)>2 else '0')+'.'
    tree=ET.fromstring(fetch('https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml',limit=4*1024*1024))
    versions=[x.text for x in tree.findall('.//version') if x.text and x.text.startswith(prefix)]
    if not versions:raise ValueError('Nebyl nalezen build NeoForge pro Minecraft '+version)
    versions.sort(key=lambda s:(tuple(int(n) for n in re.findall(r'\d+',s)[:3]),'-beta' not in s and '-alpha' not in s))
    return versions[-1]
def create(d):
    name=str(d.get('name','')).strip();kind=d.get('kind');ver=d.get('version');port=int(d.get('port',0));ram=int(d.get('ram',0))
    if not name or len(name)>48 or any(x in name for x in '/\\\r\n'):raise ValueError('Neplatný název')
    if kind not in KINDS or not VER.fullmatch(str(ver)):raise ValueError('Neplatný typ nebo verze')
    if not d.get('eula'):raise ValueError('Nejprve přijmi Minecraft EULA')
    if port<1024 or port>65535 or ram<512 or ram>131072:raise ValueError('Neplatný port nebo RAM')
    java,_=version_info(ver);sid=secrets.token_hex(8)
    with db() as c:c.execute('INSERT INTO servers(id,name,kind,version,port,ram,java,state,created) VALUES(?,?,?,?,?,?,?,?,?)',(sid,name,kind,ver,port,ram,java,'installing',int(time.time())))
    return {'id':sid,'job':job('install',sid,lambda j:install(j,sid))}
def install(jid,sid):
    s=get(sid);dest=DATA/sid;tmp=DATA/(sid+'.installing');tmp.mkdir(mode=0o770)
    try:
        java,details=version_info(s['version']);exe=f'/usr/lib/jvm/java-{java}-openjdk-amd64/bin/java'
        if java!=s['java'] or not Path(exe).exists():raise RuntimeError('Požadovaná Java chybí')
        kind=s['kind'];loader=''
        if kind=='neoforge':
            loader=choose_neo(s['version']);update(jid,'Stahuji NeoForge '+loader)
            jar=tmp/'installer.jar';fetch(f'https://maven.neoforged.net/releases/net/neoforged/neoforge/{loader}/neoforge-{loader}-installer.jar',jar)
            with (tmp/'install.log').open('w') as output:res=subprocess.run([exe,'-jar',str(jar),'--installServer'],cwd=tmp,stdout=output,stderr=subprocess.STDOUT,timeout=1500)
            if res.returncode or not (tmp/f'libraries/net/neoforged/neoforge/{loader}/unix_args.txt').exists():raise RuntimeError('NeoForge selhal; viz install.log')
            jar.unlink()
        elif kind in ('spigot','craftbukkit'):
            update(jid,'Sestavuji pomocí BuildTools (může to trvat desítky minut)')
            jar=tmp/'BuildTools.jar';fetch('https://hub.spigotmc.org/jenkins/job/BuildTools/lastSuccessfulBuild/artifact/target/BuildTools.jar',jar,30*1024*1024)
            args=[exe,'-Xmx2G','-jar',str(jar),'--rev',s['version']]+(['--compile','craftbukkit'] if kind=='craftbukkit' else [])
            with (tmp/'build.log').open('w') as output:res=subprocess.run(args,cwd=tmp,stdout=output,stderr=subprocess.STDOUT,timeout=3600)
            candidates=list(tmp.glob(kind+'-*.jar'))
            if res.returncode or not candidates:raise RuntimeError('BuildTools selhal; viz build.log')
            shutil.copy2(sorted(candidates)[-1],tmp/'server.jar')
            for folder in ('work','Bukkit','CraftBukkit','Spigot'):shutil.rmtree(tmp/folder,ignore_errors=True)
        elif kind=='paper':
            builds=json_url(f'https://fill.papermc.io/v3/projects/paper/versions/{s["version"]}/builds')
            builds=[x for x in builds if x.get('channel')=='STABLE' and x.get('downloads',{}).get('server:default',{}).get('url')]
            if not builds:raise RuntimeError('Pro verzi neexistuje stabilní Paper build')
            fetch(builds[0]['downloads']['server:default']['url'],tmp/'server.jar')
        else:
            item=details.get('downloads',{}).get('server')
            if not item:raise RuntimeError('Mojang server pro tuto verzi nenabízí')
            fetch(item['url'],tmp/'server.jar')
            if hashlib.sha1((tmp/'server.jar').read_bytes()).hexdigest()!=item['sha1']:raise RuntimeError('Nesouhlasí SHA1 ze zdroje Mojang')
        (tmp/'server.properties').write_text(f'server-port={s["port"]}\nenable-rcon=false\nenable-query=false\nonline-mode=true\n',encoding='utf8')
        (tmp/'eula.txt').write_text('eula=true\n');(tmp/'logs').mkdir(exist_ok=True);tmp.rename(dest)
        with db() as c:c.execute('UPDATE servers SET loader=?,state=? WHERE id=?',(loader,'ready',sid))
    except Exception:
        with db() as c:c.execute('UPDATE servers SET state=? WHERE id=?',('failed',sid))
        shutil.rmtree(tmp,ignore_errors=True);raise
def profile(d):
    sid=d['server'];get(sid);paths=d.get('paths')
    if not isinstance(paths,list) or not paths or len(paths)>100:raise ValueError('Vyber cesty k záloze')
    root=(DATA/sid).resolve()
    for rel in paths:
        p=path(sid,rel)
        if p==root:raise ValueError('Nelze zálohovat kořen serveru touto volbou')
    dest=Path(d.get('destination',str(ARCH))).resolve()
    if not (dest==ARCH.resolve() or str(dest).startswith('/mnt/')) or not dest.is_dir() or not os.access(dest,os.W_OK):raise ValueError('Cíl musí být zapisovatelný /srv/rift/backups nebo /mnt/…')
    hours=int(d.get('hours',24));keep=int(d.get('keep',14))
    if hours<1 or hours>8760 or keep<1 or keep>365:raise ValueError('Neplatný interval/počet kopií')
    pid=secrets.token_hex(8)
    with db() as c:c.execute('INSERT INTO profiles VALUES(?,?,?,?,?,?,?,?,?)',(pid,sid,str(d.get('name','Záloha'))[:60],json.dumps(paths),str(dest),hours,keep,int(time.time())+hours*3600,int(bool(d.get('offline',True)))))
    return pid
def backup(jid,pid):
    with db() as c:b=c.execute('SELECT * FROM profiles WHERE id=?',(pid,)).fetchone()
    if not b:raise ValueError('Profil neexistuje')
    sid=b['server']
    with LOCK:
        if sid in ACTIVE:raise ValueError('Záloha tohoto serveru už běží')
        ACTIVE.add(sid)
    was=running(sid);tmp=None
    try:
        if was and b['offline']:update(jid,'Zastavuji server kvůli konzistenci');stop(sid)
        elif was:command(sid,'save-off');command(sid,'save-all flush');time.sleep(3)
        folder=Path(b['destination'])/sid;folder.mkdir(parents=True,exist_ok=True);tmp=folder/('.'+secrets.token_hex(8)+'.tmp');target=folder/(str(int(time.time()))+'-'+pid+'.tar.gz')
        with tarfile.open(tmp,'w:gz') as tar:
            for rel in json.loads(b['paths']):
                p=path(sid,rel)
                if p.exists() and not p.is_symlink():tar.add(p,arcname=rel,filter=lambda x:None if (x.issym() or x.islnk() or x.isdev() or x.isfifo()) else x)
        tmp.replace(target);update(jid,'Uloženo: '+str(target))
        for old in sorted(folder.glob('*-'+pid+'.tar.gz'),key=lambda x:x.stat().st_mtime,reverse=True)[b['keep']:]:old.unlink()
        with db() as c:c.execute('UPDATE profiles SET next_run=? WHERE id=?',(int(time.time())+b['hours']*3600,pid))
    finally:
        if tmp:tmp.unlink(missing_ok=True)
        if was:
            if b['offline']:
                with contextlib.suppress(Exception):start(sid)
            elif running(sid):
                with contextlib.suppress(Exception):command(sid,'save-on')
        with LOCK:ACTIVE.discard(sid)
def restore(jid,sid,pid,filename):
    import tempfile
    get(sid)
    with db() as c:b=c.execute('SELECT * FROM profiles WHERE id=? AND server=?',(pid,sid)).fetchone()
    if not b or not re.fullmatch(r'[0-9]+-'+re.escape(pid)+r'\.tar\.gz',filename):raise ValueError('Archiv nenalezen')
    archive=Path(b['destination'])/sid/filename
    if not archive.is_file():raise ValueError('Archiv nenalezen')
    was=running(sid)
    if was:stop(sid)
    try:
        with tempfile.TemporaryDirectory(dir=ARCH) as t:
            stage=Path(t)
            with tarfile.open(archive,'r:gz') as tar:
                items=tar.getmembers()
                if len(items)>100000 or any(not (i.isfile() or i.isdir()) or stage not in (stage/i.name).resolve().parents for i in items):raise ValueError('Neplatný archiv')
                tar.extractall(stage,filter='data')
            rollback=ARCH/(sid+'-before-restore-'+str(int(time.time()))+'.tar.gz')
            with tarfile.open(rollback,'w:gz') as tar:
                for top in stage.iterdir():
                    old=path(sid,top.name)
                    if old.exists():tar.add(old,arcname=top.name)
            update(jid,'Záchranná záloha: '+str(rollback))
            for top in stage.iterdir():
                old=path(sid,top.name)
                if old.is_dir():shutil.rmtree(old)
                elif old.exists():old.unlink()
                shutil.move(str(top),str(old))
    finally:
        if was:start(sid)
def tasks():
    while True:
        try:
            now=int(time.time())
            with db() as c:
                due=c.execute('SELECT id,server FROM profiles WHERE next_run<=?',(now,)).fetchall()
                for row in due:c.execute('UPDATE profiles SET next_run=? WHERE id=?',(now+300,row['id']))
                rest=c.execute('SELECT id,restart_hours FROM servers WHERE restart_hours>0 AND next_restart<=? AND state=?',(now,'ready')).fetchall()
                for row in rest:c.execute('UPDATE servers SET next_restart=? WHERE id=?',(now+row['restart_hours']*3600,row['id']))
            for row in due:job('backup',row['server'],lambda j,p=row['id']:backup(j,p))
            for row in rest:
                if running(row['id']):job('restart',row['id'],lambda j,s=row['id']:(stop(s),start(s)))
        except Exception as exc:print('scheduler:',exc,flush=True)
        time.sleep(45)
def status():
    m={}
    for line in Path('/proc/meminfo').read_text().splitlines():
        if ':' in line:
            k,v=line.split(':',1);m[k]=int(v.strip().split()[0])*1024
    d=[]
    for p in ('/','/srv/rift','/mnt'):
        if Path(p).exists():
            u=shutil.disk_usage(p);d.append({'path':p,'total':u.total,'used':u.used,'free':u.free})
    ticks=[int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:]]
    total=sum(ticks);idle=ticks[3]+ticks[4];now=time.monotonic()
    rx=tx=0
    for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
        iface,values=line.split(':',1)
        if iface.strip()=='lo':continue
        numbers=values.split();rx+=int(numbers[0]);tx+=int(numbers[8])
    with LOCK:
        prev=_SAMPLE.get('host');_SAMPLE['host']=(total,idle,rx,tx,now)
    dt=max(.001,now-prev[4]) if prev else 1
    cpu_percent=round(100*(1-(idle-prev[1])/max(1,total-prev[0])),1) if prev else None
    temperature=None
    for hw in Path('/sys/class/hwmon').glob('hwmon*'):
        try:
            if (hw/'name').read_text().strip() not in ('k10temp','coretemp','cpu_thermal'):continue
            val=int((hw/'temp1_input').read_text())/1000
            if 0<val<130:temperature=round(val,1);break
        except (OSError,ValueError):pass
    return {'hostname':socket.gethostname(),'cpu':os.cpu_count(),'cpu_percent':cpu_percent,'cpu_temperature':temperature,
        'network_rx_rate':round((rx-prev[2])/dt) if prev else 0,'network_tx_rate':round((tx-prev[3])/dt) if prev else 0,
        'load':os.getloadavg(),'memory_total':m.get('MemTotal',0),'memory_used':m.get('MemTotal',0)-m.get('MemAvailable',0),
        'disks':d,'uptime':int(float(Path('/proc/uptime').read_text().split()[0]))}

def process_metrics(sid):
    get(sid);p=RUN/(sid+'.pid')
    try:
        pid=int(p.read_text());exe=Path(f'/proc/{pid}/cmdline').read_bytes()
        if b'/bin/java' not in exe and b'java' not in exe:return {}
        stats={}
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if ':' in line:
                k,v=line.split(':',1);stats[k]=v.strip()
        return {'pid':pid,'rss_bytes':int(stats.get('VmRSS','0 kB').split()[0])*1024,'threads':int(stats.get('Threads','0'))}
    except (FileNotFoundError,PermissionError,ValueError,IndexError):return {}



def players(sid):
    """Ask the server console for an authoritative list; no RCON port needed."""
    get(sid)
    if not running(sid):return {'online':0,'max':None,'names':[]}
    logfile=DATA/sid/'logs/console.log'
    before=logfile.stat().st_size if logfile.exists() else 0
    command(sid,'list')
    regex=re.compile(r'There are (\d+) of a max of (\d+) players online:\s*(.*)',re.I)
    until=time.monotonic()+4
    while time.monotonic()<until:
        if logfile.exists() and logfile.stat().st_size>before:
            with logfile.open('rb') as f:f.seek(before);text=f.read(10000).decode('utf8','replace')
            matches=regex.findall(text)
            if matches:
                count,total,raw=matches[-1]
                names=[x.strip() for x in raw.strip().split(',') if re.fullmatch(r'[A-Za-z0-9_]{3,16}',x.strip())]
                return {'online':int(count),'max':int(total),'names':names}
        time.sleep(.25)
    return {'online':None,'max':None,'names':[],'message':'Server neodpověděl na příkaz list'}
