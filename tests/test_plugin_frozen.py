"""Verify all official plugins load in the frozen host, without invoking hardware."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from plugin_pack import pack
from plugin_store import PluginStore

def run():
    root=Path(__file__).resolve().parents[1]
    binary=Path(os.environ.get('DEVMUX_TEST_EXE',str(root/'dist/plugin-release/DevMux.exe')))
    if not binary.is_file():
        raise AssertionError(f'请先构建 {binary}')
    from PyInstaller.archive.readers import CArchiveReader
    archive=CArchiveReader(str(binary))
    names={name.replace('\\','/') for name in archive.toc}
    for resource in ('renderer/plugins.js','renderer/plugins.css','renderer/desktop-plugin.js','renderer/desktop-plugin.html','plugin_sdk/sdk.js','plugin_sdk/ui.css'):
        entry=next(name for name in archive.toc if name.replace('\\','/')==resource)
        assert archive.extract(entry)==(root/resource).read_bytes(), resource

    for removed in ('renderer/usb-relay.js','renderer/mijia.js','renderer/db-flash.js','relay/RelayProbe.exe','relay/usb_relay_device.dll'):
        assert removed not in names, removed
    with tempfile.TemporaryDirectory() as temp:
        store=PluginStore(Path(temp)/'installed')
        for identifier,action in [('usb-relay','config'),('db-flash','config'),('mijia','status'),('example','echo')]:
            package=pack(root/'plugins'/identifier,Path(temp)/(identifier+'.dmp'))
            manifest=store.install(package)
            if identifier=='mijia':
                assert set(manifest.get('localActions',[]))=={'status','snapshot','read-switch','set-switch'}
                assert not manifest.get('actions')
            process=subprocess.Popen([str(binary),'--plugin-host',str(store.root/identifier)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                output,error=process.communicate(json.dumps({'action':action,'arguments':{}})+'\n',timeout=30)
                response=json.loads(output)
                assert process.returncode==0 and response['success'],(identifier,output,error)
                assert response['result'].get('success',True),(identifier,output,error)
                if identifier=='example':assert response['result']=={'text':''},(output,error)
            finally:
                if process.poll() is None:
                    process.kill();process.wait()
            print('[PASS] frozen plugin',identifier)
    return True

if __name__=='__main__':
    import sys
    sys.exit(0 if run() else 1)
