"""Single HTTPS management panel. No third-party Python dependencies."""
import hashlib,json,os,re,secrets,shutil,ssl,subprocess,sys,threading,time,urllib.parse
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import engine as e
STATIC=Path(__file__).with_name('static');FAILED={}
def digest(password,salt):return hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),320000).hex()
def create_admin(name,password):
    if not re.fullmatch(r'[a-z][a-z0-9_-]{2,30}',name) or len(password)<12:raise ValueError('Správce nebo heslo jsou neplatné')
    e.init()
    with e.db() as c:
        if c.execute('SELECT count(*) FROM users').fetchone()[0]:raise ValueError('Správce již existuje')
        salt=secrets.token_hex(16);c.execute('INSERT INTO users VALUES(?,?,?)',(name,salt,digest(password,salt)))
    (e.BASE/'local-first-setup').write_text('enabled')
def session(cookie):
    match=re.search(r'(?:^|;\s*)rift=([a-f0-9]{64})(?:;|$)',cookie)
    if not match:return None
    with e.db() as c:row=c.execute('SELECT name,csrf FROM sessions WHERE token=? AND expires>?',(hashlib.sha256(match.group(1).encode()).hexdigest(),int(time.time()))).fetchone()
    return dict(row) if row else None
def login(name,password,ip):
    t=time.time();FAILED[ip]=[v for v in FAILED.get(ip,[]) if t-v<300]
    if len(FAILED[ip])>=10:raise ValueError('Příliš mnoho pokusů, počkej pět minut')
    with e.db() as c:
        row=c.execute('SELECT * FROM users WHERE name=?',(name,)).fetchone();salt=row['salt'] if row else '00'*16
        if row is None or not secrets.compare_digest(digest(password,salt),row['digest']):FAILED[ip].append(t);raise ValueError('Nesprávné přihlašovací údaje')
        FAILED.pop(ip,None);token=secrets.token_hex(32);csrf=secrets.token_hex(24)
        c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),name,csrf,int(t)+28800))
    return token,csrf
class Handler(BaseHTTPRequestHandler):
    server_version='RiftServerOS/1.0'
    def json(self,value,code=200,headers=None):
        body=json.dumps(value,ensure_ascii=False,default=str).encode();self.send_response(code)
        for key,val in {'Content-Type':'application/json; charset=utf-8','Content-Length':str(len(body)),'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'",**(headers or {})}.items():self.send_header(key,val)
        self.end_headers();self.wfile.write(body)
    def data(self):
        length=int(self.headers.get('Content-Length','0'))
        if length<0 or length>2097152:raise ValueError('Požadavek přesáhl 2 MB')
        return json.loads(self.rfile.read(length) or '{}')
    def auth(self,write=False):
        user=session(self.headers.get('Cookie',''))
        if not user:raise PermissionError('Přihlas se')
        if write:
            if not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),user['csrf']):raise PermissionError('Neplatný bezpečnostní token')
            origin=self.headers.get('Origin')
            if origin and origin!='https://'+self.headers.get('Host'):raise PermissionError('Neplatný původ stránky')
        return user
    def handle_route(self,method):
        parsed=urllib.parse.urlsplit(self.path);p=parsed.path;q=urllib.parse.parse_qs(parsed.query)
        if method=='GET' and p in ('/','/index.html','/ui.js','/style.css'):
            file=STATIC/('index.html' if p=='/' else p.lstrip('/'));body=file.read_bytes();self.send_response(200);self.send_header('Content-Length',str(len(body)));self.send_header('Content-Type','text/html; charset=utf-8' if p in ('/','/index.html') else 'text/javascript' if p.endswith('.js') else 'text/css');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'");self.end_headers();self.wfile.write(body);return
        if p=='/api/local-setup':
            if self.client_address[0] not in ('127.0.0.1','::1'):raise PermissionError('První nastavení je dostupné pouze přímo na počítači')
            marker=e.BASE/'local-first-setup'
            if method=='GET':return self.json({'available':marker.exists()})
            if method=='POST':
                if not marker.exists():raise PermissionError('První nastavení již proběhlo')
                d=self.data();password=str(d.get('password',''))
                if len(password)<12:raise ValueError('Heslo musí mít alespoň 12 znaků')
                salt=secrets.token_hex(16)
                with e.db() as c:c.execute('UPDATE users SET salt=?,digest=? WHERE name=?',(salt,digest(password,salt),'admin'))
                marker.unlink();return self.json({'ok':True})
        if p=='/api/login' and method=='POST':
            d=self.data();token,csrf=login(str(d.get('name','')),str(d.get('password','')),self.client_address[0]);return self.json({'csrf':csrf},headers={'Set-Cookie':'rift='+token+'; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'})
        user=self.auth(method!='GET')
        if p=='/api/me':return self.json(user)
        if p=='/api/logout' and method=='POST':
            match=re.search(r'rift=([a-f0-9]{64})',self.headers.get('Cookie',''))
            if match:
                with e.db() as c:c.execute('DELETE FROM sessions WHERE token=?',(hashlib.sha256(match.group(1).encode()).hexdigest(),))
            return self.json({'ok':True},headers={'Set-Cookie':'rift=; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
        if p=='/api/password' and method=='POST':
            d=self.data();old=d.get('old','');new=d.get('new','')
            if len(new)<12:raise ValueError('Nové heslo musí mít alespoň 12 znaků')
            with e.db() as c:
                row=c.execute('SELECT * FROM users WHERE name=?',(user['name'],)).fetchone()
                if not secrets.compare_digest(digest(old,row['salt']),row['digest']):raise ValueError('Staré heslo nesouhlasí')
                salt=secrets.token_hex(16);c.execute('UPDATE users SET salt=?,digest=? WHERE name=?',(salt,digest(new,salt),user['name']))
                c.execute('DELETE FROM sessions WHERE name=? AND csrf<>?',(user['name'],user['csrf']))
            return self.json({'ok':True})
        if p=='/api/servers':
            if method=='GET':return self.json(e.servers())
            return self.json(e.create(self.data()),201)
        if p=='/api/versions':return self.json(e.available(q.get('kind',['neoforge'])[0]))
        if p=='/api/status':return self.json(e.status())
        if p=='/api/disks':return self.json(e.privileged('disks'))
        if p=='/api/mount-backup' and method=='POST':return self.json(e.privileged('mount_backup',device=self.data().get('device','')))
        if p=='/api/jobs':
            with e.db() as c:rows=[dict(x) for x in c.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 30')]
            return self.json(rows)
        if p=='/api/services':
            if method=='GET':return self.json({s:subprocess.run(['/usr/bin/systemctl','is-active',s],capture_output=True,text=True,timeout=4).stdout.strip() for s in ('mariadb.service','ssh.service','vsftpd.service','rift-panel.service','rift-kiosk.service')})
            d=self.data();return self.json(e.privileged('service',name=d.get('name'),verb=d.get('verb')))
        if p=='/api/database':
            if method=='GET':return self.json(e.privileged('db_list'))
            return self.json(e.privileged('db_create',name=self.data().get('name','')))
        if p=='/api/power' and method=='POST':
            d=self.data();verb=d.get('verb')
            if verb not in ('reboot','poweroff'):raise ValueError('Neplatná akce')
            if d.get('backup'):
                for server in e.servers():
                    if not server['running']:continue
                    with e.db() as c:p=c.execute('SELECT id FROM profiles WHERE server=? ORDER BY next_run LIMIT 1',(server['id'],)).fetchone()
                    if not p:raise ValueError('Server '+server['name']+' nemá profil zálohy; vypnutí bylo zrušeno')
                    jid=secrets.token_hex(8)
                    with e.db() as c:c.execute('INSERT INTO jobs(id,server,kind,state,created) VALUES(?,?,?,?,?)',(jid,server['id'],'backup-before-power','running',int(time.time())))
                    try:e.backup(jid,p['id']);e.update(jid,'Hotovo','done')
                    except Exception as ex:e.update(jid,str(ex),'failed');raise
            for server in e.servers():
                if server['running']:e.stop(server['id'])
            return self.json(e.privileged('power',verb=verb))
        parts=p.strip('/').split('/')
        if len(parts)>=3 and parts[:2]==['api','server']:
            sid=parts[2];s=e.get(sid);tail='/'.join(parts[3:])
            if tail=='':
                state=e.server_status(sid)
                return self.json({**s,'status':state,'running':state=='online'})
            if tail=='players':return self.json(e.players(sid))
            if tail=='process':return self.json(e.process_metrics(sid))
            if tail=='console':
                file=e.DATA/sid/'logs/console.log'
                if not file.exists():return self.json({'log':''})
                with file.open('rb') as f:f.seek(0,2);f.seek(max(0,f.tell()-130000));data=f.read()
                return self.json({'log':'\n'.join(data.decode('utf8','replace').splitlines()[-450:])})
            if tail=='command' and method=='POST':return self.json(e.command(sid,self.data().get('value','')))
            if tail=='action' and method=='POST':
                verb=self.data().get('verb')
                return self.json(e.control(sid,verb))
            if tail=='delete' and method=='POST':
                d=self.data()
                return self.json(e.delete_server(sid,d.get('name'),d.get('remove_backups') is True))
            if tail=='autostart' and method=='POST':
                enabled=bool(self.data().get('enabled'))
                e.privileged('service',name='rift-mc@'+sid+'.service',verb='enable' if enabled else 'disable')
                with e.db() as c:c.execute('UPDATE servers SET autostart=? WHERE id=?',(int(enabled),sid))
                return self.json({'ok':True})
            if tail=='schedule' and method=='POST':
                hours=int(self.data().get('hours',0))
                if hours<0 or hours>8760:raise ValueError('Neplatný interval')
                with e.db() as c:c.execute('UPDATE servers SET restart_hours=?,next_restart=? WHERE id=?',(hours,int(time.time())+hours*3600 if hours else 0,sid))
                return self.json({'ok':True})
            if tail=='files' and method=='GET':
                rel=q.get('path',[''])[0];folder=e.path(sid,rel)
                if not folder.is_dir():raise ValueError('Složka neexistuje')
                return self.json({'path':rel,'items':[{'name':i.name,'dir':i.is_dir(),'size':i.stat().st_size} for i in sorted(folder.iterdir(),key=lambda x:(not x.is_dir(),x.name.lower())) if not i.is_symlink()][:2000]})
            if tail=='file' and method=='GET':
                file=e.path(sid,q.get('path',[''])[0])
                if file.suffix.lower() not in ('.txt','.json','.toml','.yml','.yaml','.properties','.cfg','.conf','.log','.mcmeta') or not file.is_file() or file.stat().st_size>2097152:raise ValueError('Nepodporovaný textový soubor (max. 2 MB)')
                return self.json({'content':file.read_text(encoding='utf8')})
            if tail=='file' and method=='POST':
                d=self.data();file=e.path(sid,d.get('path',''));text=str(d.get('content',''))
                if file.suffix.lower() not in ('.txt','.json','.toml','.yml','.yaml','.properties','.cfg','.conf','.log','.mcmeta') or not file.is_file() or len(text.encode())>2097152:raise ValueError('Soubor nelze upravit')
                temp=file.with_name(file.name+'.rift-tmp');temp.write_text(text,encoding='utf8');temp.replace(file);return self.json({'ok':True})
            if tail=='file-action' and method=='POST':
                d=self.data();file=e.path(sid,d.get('path',''))
                if file==(e.DATA/sid).resolve():raise ValueError('Nelze změnit kořen serveru')
                if d['verb']=='mkdir':file.mkdir()
                elif d['verb']=='delete':shutil.rmtree(file) if file.is_dir() else file.unlink()
                elif d['verb']=='rename':
                    target=e.path(sid,d.get('target',''))
                    if target.exists() or target==(e.DATA/sid).resolve():raise ValueError('Cíl už existuje')
                    file.rename(target)
                else:raise ValueError('Neznámá akce')
                return self.json({'ok':True})
            if tail=='upload' and method=='POST':
                file=e.path(sid,q.get('path',[''])[0]);count=int(self.headers.get('Content-Length','0'))
                if file==(e.DATA/sid).resolve() or file.exists() or count<1 or count>134217728:raise ValueError('Soubor již existuje nebo je příliš velký (128 MB)')
                file.parent.mkdir(parents=True,exist_ok=True)
                try:
                    with file.open('xb') as out:
                        while count:
                            block=self.rfile.read(min(65536,count))
                            if not block:raise ValueError('Přenos byl přerušen')
                            out.write(block);count-=len(block)
                except Exception:file.unlink(missing_ok=True);raise
                return self.json({'ok':True})
            if tail=='download' and method=='GET':
                file=e.path(sid,q.get('path',[''])[0])
                if not file.is_file() or file.stat().st_size>536870912:raise ValueError('Limit stažení 512 MB')
                body=file.read_bytes();self.send_response(200);self.send_header('Content-Type','application/octet-stream');self.send_header('Content-Length',str(len(body)));self.send_header('Content-Disposition','attachment; filename="'+file.name.replace('"','')+'"');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(body);return
        if p=='/api/profiles':
            if method=='POST':return self.json({'id':e.profile(self.data())},201)
            with e.db() as c:items=[dict(row) for row in c.execute('SELECT * FROM profiles ORDER BY next_run')]
            for item in items:item['paths']=json.loads(item['paths'])
            return self.json(items)
        if p=='/api/backup/run' and method=='POST':
            pid=self.data().get('id')
            with e.db() as c:b=c.execute('SELECT server FROM profiles WHERE id=?',(pid,)).fetchone()
            if not b:raise ValueError('Profil neexistuje')
            return self.json({'job':e.job('backup',b['server'],lambda j:e.backup(j,pid))})
        if p=='/api/backup/delete' and method=='POST':
            with e.db() as c:c.execute('DELETE FROM profiles WHERE id=?',(self.data().get('id'),))
            return self.json({'ok':True})
        if p=='/api/backup/archive':
            sid=q.get('server',[''])[0];e.get(sid)
            with e.db() as c:profiles=c.execute('SELECT id,destination FROM profiles WHERE server=?',(sid,)).fetchall()
            items=[]
            for row in profiles:
                for file in (Path(row['destination'])/sid).glob('*-'+row['id']+'.tar.gz'):
                    items.append({'profile':row['id'],'name':file.name,'size':file.stat().st_size})
            return self.json(sorted(items,key=lambda x:x['name'],reverse=True))
        if p=='/api/backup/restore' and method=='POST':
            d=self.data();sid=d['server'];return self.json({'job':e.job('restore',sid,lambda j:e.restore(j,sid,d['profile'],d['name']))})
        raise ValueError('Neznámá cesta')
    def do_GET(self):self.safe('GET')
    def do_POST(self):self.safe('POST')
    def safe(self,method):
        try:self.handle_route(method)
        except PermissionError as exc:self.json({'error':str(exc)},401)
        except (ValueError,KeyError,FileNotFoundError,FileExistsError,IsADirectoryError) as exc:self.json({'error':str(exc)},400)
        except Exception as exc:print('ERROR',repr(exc),flush=True);self.json({'error':str(exc)},500)
def main():
    e.init();threading.Thread(target=e.tasks,daemon=True).start()
    http=ThreadingHTTPServer((os.getenv('RIFT_HOST','0.0.0.0'),int(os.getenv('RIFT_PORT','8443'))),Handler)
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.minimum_version=ssl.TLSVersion.TLSv1_2;ctx.load_cert_chain(os.getenv('RIFT_CERT','/etc/rift/tls/server.crt'),os.getenv('RIFT_KEY','/etc/rift/tls/server.key'));http.socket=ctx.wrap_socket(http.socket,server_side=True);http.serve_forever()
if __name__=='__main__':
    if len(sys.argv)==2 and sys.argv[1]=='init-admin':
        user=json.load(sys.stdin);create_admin(user['name'],user['password'])
    else:main()
