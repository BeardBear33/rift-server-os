"""Small privileged helper with explicit action and name allowlists."""
import json,os,re,socketserver,subprocess
import engine as e
SERVICES=('mariadb.service','ssh.service','vsftpd.service','rift-panel.service','rift-kiosk.service')
class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            request=json.loads(self.rfile.readline(8192));action=request.get('action')
            if action=='service':
                unit=str(request.get('name',''));verb=request.get('verb')
                if unit not in SERVICES and not re.fullmatch(r'rift-mc@[a-f0-9]{16}\.service',unit):raise ValueError('Nepovolená služba')
                if verb not in ('start','stop','restart','enable','disable'):raise ValueError('Nepovolená operace')
                if unit.startswith('rift-mc@'):
                    s=e.get(unit[8:-8])
                    if verb=='start' and s['state']!='ready':raise ValueError('Server ještě není připraven')
                out=subprocess.run(['/usr/bin/systemctl',verb,unit],capture_output=True,text=True,timeout=125)
                if out.returncode:raise RuntimeError(out.stderr[-600:] or 'systemctl selhal')
                result={'ok':True}
            elif action=='power':
                verb=request.get('verb')
                if verb not in ('reboot','poweroff'):raise ValueError('Nepovolená operace')
                subprocess.Popen(['/usr/bin/systemctl',verb],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);result={'ok':True}
            elif action=='disks':
                data=subprocess.run(['/usr/bin/lsblk','-J','-b','-o','NAME,TYPE,SIZE,MODEL,MOUNTPOINT,FSTYPE'],capture_output=True,text=True,timeout=10)
                if data.returncode:raise RuntimeError('Nelze číst informace o discích')
                drives=json.loads(data.stdout).get('blockdevices',[])
                for drive in drives:
                    if drive.get('type')!='disk':continue
                    smart=subprocess.run(['/usr/sbin/smartctl','-H','/dev/'+drive['name']],capture_output=True,text=True,timeout=12)
                    info=smart.stdout+smart.stderr
                    drive['health']='OK' if ('PASSED' in info or 'SMART Health Status: OK' in info) else 'Neznámý' if ('Unavailable' in info or 'not available' in info or 'Permission denied' in info) else 'Zkontrolovat'
                result={'ok':True,'disks':drives}
            elif action=='mount_backup':
                name=str(request.get('device',''))
                if not re.fullmatch(r'[a-zA-Z0-9_-]{2,40}',name):raise ValueError('Neplatný diskový oddíl')
                device='/dev/'+name
                if not os.path.exists(device):raise ValueError('Oddíl neexistuje')
                kind=subprocess.check_output(['/usr/bin/lsblk','-dn','-o','TYPE',device],text=True,timeout=5).strip()
                if kind!='part':raise ValueError('Vyber oddíl, nikoli celý disk')
                fs=subprocess.check_output(['/usr/bin/lsblk','-dn','-o','FSTYPE',device],text=True,timeout=5).strip()
                mounted=subprocess.check_output(['/usr/bin/lsblk','-dn','-o','MOUNTPOINT',device],text=True,timeout=5).strip()
                if fs not in ('ext4','xfs','btrfs') or mounted:raise ValueError('Oddíl musí mít Linux souborový systém a nesmí být připojen')
                uuid=subprocess.check_output(['/usr/sbin/blkid','-s','UUID','-o','value',device],text=True,timeout=5).strip()
                if not re.fullmatch(r'[a-zA-Z0-9-]{8,64}',uuid):raise ValueError('UUID nenalezeno')
                target='/mnt/rift-backups';os.makedirs(target,exist_ok=True)
                if subprocess.run(['/usr/bin/mountpoint','-q',target]).returncode==0:raise ValueError('Cílová složka již obsahuje připojený disk')
                if 'UUID='+uuid in open('/etc/fstab').read():raise ValueError('Disk je již uveden v /etc/fstab')
                with open('/etc/fstab','a') as f:f.write('\nUUID='+uuid+' '+target+' '+fs+' defaults,nofail 0 2\n')
                try:subprocess.run(['/usr/bin/mount',target],check=True,timeout=20)
                except Exception:
                    # On failure remove only our appended entry.
                    lines=open('/etc/fstab').readlines()
                    with open('/etc/fstab','w') as f:f.writelines([line for line in lines if not line.startswith('UUID='+uuid+' '+target+' ')])
                    raise
                import pwd
                rift=pwd.getpwnam('rift');os.chown(target,rift.pw_uid,rift.pw_gid);os.chmod(target,0o770)
                result={'ok':True,'path':target}
            elif action=='db_list':
                out=subprocess.run(['/usr/bin/mariadb','--batch','--skip-column-names','-e','SHOW DATABASES'],capture_output=True,text=True,timeout=10)
                if out.returncode:raise RuntimeError(out.stderr[-300:])
                result={'ok':True,'databases':[n for n in out.stdout.splitlines() if n not in ('mysql','sys','information_schema','performance_schema')]}
            elif action=='db_create':
                name=str(request.get('name',''))
                if not e.NAME.fullmatch(name):raise ValueError('Název: 3–32 písmen, číslic nebo _')
                pw=__import__('secrets').token_urlsafe(24)
                sql=f"CREATE DATABASE `{name}`; CREATE USER '{name}'@'localhost' IDENTIFIED BY '{pw}'; GRANT ALL PRIVILEGES ON `{name}`.* TO '{name}'@'localhost';"
                out=subprocess.run(['/usr/bin/mariadb','-e',sql],capture_output=True,text=True,timeout=20)
                if out.returncode:raise RuntimeError(out.stderr[-300:])
                result={'ok':True,'database':name,'username':name,'password':pw}
            else:raise ValueError('Neznámá operace')
        except Exception as exc:result={'ok':False,'error':str(exc)}
        self.wfile.write((json.dumps(result,ensure_ascii=False)+'\n').encode())
class Server(socketserver.ThreadingUnixStreamServer):daemon_threads=True
if __name__=='__main__':
    e.RUN.mkdir(parents=True,exist_ok=True);p=e.RUN/'root.sock';p.unlink(missing_ok=True)
    with Server(str(p),Handler) as server:os.chmod(p,0o660);server.serve_forever()
