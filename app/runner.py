"""Private stdin console, one process per server."""
import contextlib,json,os,signal,socket,subprocess,sys,threading,time
from pathlib import Path
import engine as e

def main(sid):
    s=e.get(sid)
    if s['state']!='ready':raise RuntimeError('Server není připraven')
    folder=e.DATA/sid;java=f'/usr/lib/jvm/java-{s["java"]}-openjdk-amd64/bin/java';argv=[java,f'-Xms{min(s["ram"],1024)}M',f'-Xmx{s["ram"]}M']
    if s['kind']=='neoforge':
        arg=folder/'libraries/net/neoforged/neoforge'/s['loader']/'unix_args.txt'
        if not arg.is_file():raise FileNotFoundError(arg)
        argv+=['@'+str(arg),'nogui']
    else:argv+=['-jar','server.jar','nogui']
    e.RUN.mkdir(parents=True,exist_ok=True);sockpath=e.RUN/(sid+'.sock');sockpath.unlink(missing_ok=True)
    log=folder/'logs/console.log';log.parent.mkdir(exist_ok=True)
    with log.open('ab',buffering=0) as output:
        output.write(('[Rift] Start '+time.strftime('%F %T')+'\n').encode())
        proc=subprocess.Popen(argv,cwd=folder,stdin=subprocess.PIPE,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        pidfile=e.RUN/(sid+'.pid');pidfile.write_text(str(proc.pid))
        def send(value):
            if proc.poll() is not None:return {'ok':False,'error':'Server neběží'}
            if not isinstance(value,str) or not value or len(value)>400 or '\n' in value or '\r' in value:return {'ok':False,'error':'Neplatný příkaz'}
            try:proc.stdin.write((value+'\n').encode());proc.stdin.flush();return {'ok':True}
            except OSError:return {'ok':False,'error':'Příkaz nešel odeslat'}
        def terminate(*args):send('stop')
        signal.signal(signal.SIGTERM,terminate)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(sockpath));os.chmod(sockpath,0o600);listener.listen(16);listener.settimeout(.5)
            def serve():
                while proc.poll() is None:
                    try:conn,_=listener.accept()
                    except socket.timeout:continue
                    except OSError:break
                    with conn:
                        conn.settimeout(3)
                        try:
                            obj=json.loads(conn.recv(4096).split(b'\n')[0]);reply=send(obj.get('command'))
                        except Exception as ex:reply={'ok':False,'error':str(ex)}
                        with contextlib.suppress(OSError):conn.sendall((json.dumps(reply)+'\n').encode())
            threading.Thread(target=serve,daemon=True).start()
            code=proc.wait();output.write(('[Rift] Exit '+str(code)+'\n').encode())
        sockpath.unlink(missing_ok=True);pidfile.unlink(missing_ok=True);return code
if __name__=='__main__':sys.exit(main(sys.argv[1]))
