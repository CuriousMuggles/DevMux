"""闲置清理专项：临时目录、真实文件/节点，无现场设备操作。"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import relay
import relay_app
import relay_archive
import relay_cache
from tests.test_relay_clear_cache import make_history


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='DevMux-idle-cache-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.share = self.base / 'share'
        self.share.mkdir()
        self.config = self.base / 'local' / 'relay.json'
        self.config.parent.mkdir()
        relay_app.save_config(str(self.config), relay_app.RelayConfig(
            root=str(self.share), peerId='cache-test', nodeId='node-cache', role='master', enabled=False))
        self.controller = relay_app.RelayController(config_path=str(self.config), local_devices_enabled=False)
        self.addCleanup(self.controller.stop)
        self.clock = self.file('peers/cache-test.json', b'{}', age=0).stat().st_mtime

    def file(self, name, data=b'x', age=1200):
        path = self.share / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
        clock = getattr(self, 'clock', time.time())
        os.utime(path, (clock - age, clock - age))
        return path

    def clean(self, **kwargs):
        return relay.clear_cache_root(str(self.share), peer_id='cache-test', **kwargs)

    def test_other_peers_and_pools_and_newer_than_day_are_cleaned(self):
        paths = [make_history(self.share, peer=peer, pool_id=pool, age_s=60)
                 for peer, pool in [('cache-test',''), ('other',''), ('third','lab-a')]]
        result = self.clean()
        self.assertTrue(result['success'], result)
        self.assertEqual(result['removedFiles'], 3)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(result['freedBytes'], result['scannedBytes']-result['keptBytes'])

    def test_recent_running_and_active_counterpart_are_preserved(self):
        recent = make_history(self.share, session='sess-new', age_s=10)
        running = make_history(self.share, session='sess-running', status='running')
        old = make_history(self.share, session='sess-active')
        meta = self.file('streams/cache-test/sess-active.meta.json', {'status':'running','streamId':'sess-active'})
        output = self.file('streams/cache-test/sess-active.out', b'ACTIVE'*1000)
        before = {path:path.read_bytes() for path in (recent,running,old,meta,output)}
        result = self.clean()
        self.assertTrue(result['success'], result)
        self.assertEqual({path:path.read_bytes() for path in before}, before)
        self.assertEqual(result['removedFiles'],0)

    def test_ended_stream_tail_window_and_group_cleanup(self):
        data = [self.file('streams/cache-test/sess-closed'+suffix, payload, age=60)
                for suffix,payload in [('.out',b'old'*100),('.in',b'input'),('.meta.json',{'status':'closed'})]]
        result = self.clean()
        self.assertEqual(result['removedFiles'],3,result)
        self.assertTrue(all(not path.exists() for path in data))
        self.file('streams/cache-test/sess-tail.meta.json',{'status':'ended'},age=60)
        latest = self.file('streams/cache-test/sess-tail.out',b'last-output',age=5)
        self.assertEqual(self.clean()['removedFiles'],0)
        self.assertTrue(latest.exists())

    def test_crash_stream_only_removed_when_owner_and_pulse_are_idle(self):
        self.file('peers/retired.json',{'peerId':'retired'})
        stale = self.file('streams/retired/sess-orphan.out',b'orphan'*100)
        self.file('streams/retired/sess-orphan.meta.json',{'status':'running'})
        pulse = self.file('peers/retired.pulse-42-123456abcdef',b'beat',age=1)
        self.assertEqual(self.clean()['removedFiles'],0)
        os.utime(pulse,(self.clock-1200,self.clock-1200))
        result = self.clean()
        self.assertTrue(result['success'],result)
        self.assertFalse(stale.exists())
        self.assertFalse(pulse.exists())

    def test_unknown_and_identity_and_active_journals_are_unchanged(self):
        paths = [self.file(name,b'unknown-data') for name in
                 ('user.img','archive/cache-test/business.bin','streams/retired/custom.out',
                  'requests/cache-test/.from-owner.jsonl','results/cache-test/.replies.jsonl',
                  'peers/retired.json','registry/node.json','relay.json')]
        paths.append(self.file('streams/retired/custom.meta.json',{'status':'closed'}))
        before = {path:path.read_bytes() for path in paths}
        self.assertTrue(self.clean()['success'])
        self.assertEqual({path:path.read_bytes() for path in paths},before)

    def test_expired_requests_deleted_long_ttl_and_inflight_preserved(self):
        request = relay.new_request(relay.KIND_EXEC,'master',ttl_s=30)
        request.reqId = 'oldrequest'
        expired = self.file('requests/slave/r-expired.json',request.to_dict())
        request.reqId = 'longrequest'
        request.expiresAt = request.createdAt[:0] + (relay.datetime.now(relay.timezone.utc)+relay.timedelta(days=1)).isoformat()
        ongoing = self.file('requests/slave/r-long.json',request.to_dict())
        request.reqId='busyrequest';request.expiresAt=relay.new_request('exec','master',ttl_s=30).expiresAt
        busy=self.file('requests/slave/r-busy.json',request.to_dict())
        result=self.clean(active_requests=lambda:{'busyrequest'})
        self.assertEqual(result['removedFiles'],1,result)
        self.assertFalse(expired.exists())
        self.assertTrue(ongoing.exists());self.assertTrue(busy.exists())

    def test_old_results_trimmed_but_restart_dedup_survives(self):
        queue=relay.RelayQueue.for_peer(str(self.share),'slave');queue.ensure_layout()
        req_id='a'*16
        result=relay.Result(reqId=req_id,status=relay.STATUS_OK,stdout='A'*200000,extra={'large':'X'*20000})
        path=self.file('results/slave/'+req_id+'.json',result.to_dict())
        recent=self.file('results/slave/'+'b'*16+'.json',result.to_dict()|{'reqId':'b'*16},age=10)
        old_bytes=path.stat().st_size
        cleaned=self.clean()
        self.assertEqual(cleaned['trimmedFiles'],1,cleaned)
        self.assertGreater(cleaned['freedBytes'],200000)
        self.assertLess(path.stat().st_size,old_bytes/100)
        self.assertEqual(json.loads(path.read_text())['reqId'],req_id)
        self.assertTrue(relay.RelayQueue.for_peer(str(self.share),'slave').has_result(req_id))
        self.assertTrue(recent.exists())
        again=self.clean();self.assertEqual(again['trimmedFiles'],0)

    def test_compact_large_payload_removed_index_preserved_after_restart(self):
        req_id='c'*16
        path=self.share/'archive/slave/2026-10'/('compact-'+'d'*32+'.zip')
        path.parent.mkdir(parents=True)
        index=json.dumps({'schema':1,'records':[{'reqId':req_id,'result':True}]}).encode()
        with zipfile.ZipFile(path,'w') as package:
            package.writestr('__index__.json',index)
            package.writestr('records/payload.json',b'X'*300000)
        os.utime(path,(self.clock-1200,self.clock-1200))
        before=path.stat().st_size
        self.assertTrue(relay.RelayQueue.for_peer(str(self.share),'slave').has_result(req_id))
        result=self.clean()
        self.assertTrue(result['success'],result)
        self.assertEqual(result['trimmedFiles'],1)
        self.assertGreater(result['freedBytes'],before*.99)
        with zipfile.ZipFile(path) as package:
            self.assertEqual(package.namelist(),['__index__.json'])
            self.assertEqual(package.read('__index__.json'),index)
        relay_archive._CACHE.clear();relay_archive._BUNDLE_CACHE.clear()
        self.assertTrue(relay.RelayQueue.for_peer(str(self.share),'slave').has_result(req_id))

    def test_bad_indexes_and_recent_protocol_results_are_preserved(self):
        path=self.share/'archive/slave/2026-10'/('compact-'+'e'*32+'.zip')
        path.parent.mkdir(parents=True)
        with zipfile.ZipFile(path,'w') as package:
            package.writestr('__index__.json','{"schema":9,"records":[]}')
            package.writestr('records/data',b'x'*10000)
        os.utime(path,(self.clock-1200,self.clock-1200));before=path.read_bytes()
        self.assertTrue(self.clean()['success'])
        self.assertEqual(path.read_bytes(),before)

    def test_changed_archive_retained_before_delete(self):
        old=make_history(self.share)
        real=zipfile.ZipFile
        class MutatingZip(real):
            def __exit__(self,*args):
                result=super().__exit__(*args)
                with old.open('ab') as handle:handle.write(b'changed')
                return result
        with patch.object(zipfile,'ZipFile',MutatingZip):
            result=self.clean()
        self.assertEqual(result['removedFiles'],0,result)
        self.assertTrue(old.exists())

    def test_more_than_thousand_sessions_and_error_details_bounded(self):
        for number in range(1005):make_history(self.share,session=f'sess-many-{number}',age_s=60)
        result=self.clean()
        self.assertEqual(result['removedFiles'],1005,result)
        for number in range(25):make_history(self.share,session=f'sess-locked-{number}')
        with patch.object(relay_cache.os,'unlink',side_effect=PermissionError('模拟占用')):
            failed=self.clean()
        self.assertFalse(failed['success']);self.assertEqual(failed['errorCount'],25)
        self.assertEqual(len(failed['errors']),20)

    def test_scan_budget_and_missing_clock_are_explicit(self):
        for number in range(5):make_history(self.share,session=f'sess-budget-{number}')
        with patch.object(relay_cache,'SCAN_LIMIT',3):result=self.clean()
        self.assertTrue(result['limited']);self.assertIn('未扫描',result['notice'])
        (self.share/'peers/cache-test.json').unlink()
        result=self.clean();self.assertEqual(result['removedFiles'],0)
        self.assertIn('时钟',result['notice'])

    def test_shared_clock_not_local_wall_clock(self):
        old=make_history(self.share,age_s=60)
        with patch.object(relay_cache.time,'time',side_effect=AssertionError('不比较墙钟')):
            result=self.clean()
        self.assertEqual(result['removedFiles'],1,result)
        self.assertFalse(old.exists())

    def test_junction_not_followed_and_wrong_root_rejected(self):
        if os.name!='nt':self.skipTest('Windows目录联接点')
        outside=self.base/'outside';outside.mkdir()
        keep=outside/'keep.txt';keep.write_bytes(b'keep')
        link=self.share/'archive';proc=subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],
            capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(proc.returncode,0)
        self.assertFalse(self.clean()['success'])
        self.assertEqual(keep.read_bytes(),b'keep')
        for root,confirmed in [(str(self.share),False),(str(self.base/'other'),True)]:
            with self.assertRaises(ValueError):self.controller.clear_cache(root,confirmed)
        for value in ['', '.',os.path.abspath(os.sep),str(Path.home())]:
            with self.assertRaises(ValueError):relay.validate_cache_root(value)

    def test_live_node_config_and_terminal_files_stay_unchanged(self):
        ok,reason=self.controller.apply_config({'enabled':True})
        self.assertTrue(ok,reason);node=self.controller.node
        make_history(self.share,peer=node.peer_id,age_s=60)
        meta=self.file('streams/'+node.peer_id+'/sess-live.meta.json',{'status':'running'})
        output=self.file('streams/'+node.peer_id+'/sess-live.out',b'live')
        before=(self.config.read_bytes(),meta.read_bytes(),output.read_bytes())
        result=self.controller.clear_cache(str(self.share),True)
        self.assertTrue(result['success'],result)
        self.assertIs(self.controller.node,node);self.assertTrue(node.is_running())
        self.assertEqual((self.config.read_bytes(),meta.read_bytes(),output.read_bytes()),before)
        self.assertIn('减少',result['output']);self.assertIn('保留原因',result['output'])

    def test_same_size_same_mtime_replacement_keeps_new_file(self):
        path=make_history(self.share)
        original=zipfile.ZipFile
        class ReplacingZip(original):
            def __exit__(self,*args):
                value=super().__exit__(*args)
                info=path.stat();replacement=path.with_suffix('.replacement')
                replacement.write_bytes(path.read_bytes())
                os.utime(replacement,ns=(info.st_atime_ns,info.st_mtime_ns))
                os.replace(replacement,path)
                return value
        with patch.object(zipfile,'ZipFile',ReplacingZip):result=self.clean()
        self.assertEqual(result['removedFiles'],0,result)
        self.assertTrue(path.exists())

    def test_unknown_large_files_contribute_to_retained_size(self):
        large=self.file('user-data/image.bin',b'x'*1000000)
        before=large.read_bytes();result=self.clean()
        self.assertTrue(result['success'],result)
        self.assertGreaterEqual(result['keptBytes'],1000000)
        self.assertEqual(large.read_bytes(),before)

    def test_idle_transfer_packages_cleaned_but_online_endpoints_preserved(self):
        transfer='a'*32
        path=self.file('archive/retired/transfers/source/'+transfer+'/download.zip',b'x'*1000000)
        recent=self.file('archive/retired/transfers/source/'+'b'*32+'/upload.partial',b'copying',age=5)
        heartbeat=self.file('peers/source.json',{'peerId':'source'},age=0)
        result=self.clean();self.assertTrue(path.exists())
        os.utime(heartbeat,(self.clock-1200,self.clock-1200))
        result=self.clean()
        self.assertFalse(path.exists());self.assertTrue(recent.exists())
        self.assertGreaterEqual(result['freedBytes'],1000000)

    def test_interrupted_session_partial_requires_idle_owner(self):
        path=self.file('archive/cache-test/sess-aborted.zip.partial',b'x'*100000)
        self.assertEqual(self.clean()['removedFiles'],0)
        other=self.file('archive/retired/sess-aborted.zip.partial',b'x'*100000)
        result=self.clean();self.assertFalse(other.exists());self.assertTrue(path.exists())

    def test_live_remote_terminal_keeps_same_session_and_io_after_clean(self):
        from tests.test_relay_app import _Harness, _patch, _fake_list_devices, _fake_execute, _wait_until
        harness=_Harness(str(self.base/'live-relay'),master_local_devices_enabled=False)
        try:
            with _patch(list_devices=_fake_list_devices,execute=_fake_execute):
                harness.start();harness.confirm_both()
                self.assertTrue(_wait_until(lambda: harness.backend.handle_for('devB:tid=1'),8))
                self.assertTrue(_wait_until(lambda:any(item.get('opened') for item in harness.master.status()['sessions']),8))
                handle=harness.backend.handle_for('devB:tid=1')
                key=harness.remote_key()
                before_nodes=(harness.master.node,harness.slave.node)
                before_configs=[Path(item.config_path).read_bytes() for item in (harness.master,harness.slave)]
                old=make_history(Path(harness.root),peer='pc-b',session='sess-unused',age_s=60)
                result=harness.master.clear_cache(harness.root,True)
                self.assertTrue(result['success'],result)
                self.assertFalse(old.exists())
                self.assertEqual((harness.master.node,harness.slave.node),before_nodes)
                self.assertEqual([Path(item.config_path).read_bytes() for item in (harness.master,harness.slave)],before_configs)
                self.assertEqual(harness.backend.handle_for('devB:tid=1'),handle)
                self.assertTrue(harness.backend.handles[handle]['alive'])
                self.assertTrue(harness.master.session_input(key,b'AFTER_IDLE_CLEAN\r'))
                self.assertTrue(_wait_until(lambda:b'AFTER_IDLE_CLEAN\r' in harness.backend.written(handle),6))
                marker=b'AFTER_CLEAN_REPLY\r\n';harness.backend.emit(handle,marker)
                self.assertTrue(_wait_until(lambda:marker in harness.output_text(),6))
                self.assertEqual(harness.backend.written(handle).count(b'AFTER_IDLE_CLEAN\r'),1)
        finally:
            harness.stop()

    def test_small_dedup_results_not_rewritten(self):
        value=relay.Result(reqId='d'*16,status=relay.STATUS_OK,stdout='small')
        path=self.file('results/slave/'+'d'*16+'.json',value.to_dict())
        before=path.read_bytes();result=self.clean()
        self.assertEqual(result['trimmedFiles'],0,result)
        self.assertEqual(path.read_bytes(),before)
        self.assertIn('小型去重记录',result['keptReasons'])

    def test_win32_uncached_stat_exposes_real_file_id(self):
        if os.name!='nt':self.skipTest('Windows原生文件信息')
        import relay_file_io
        path=self.file('sample.txt')
        native=relay_file_io._stat_uncached(str(path))
        self.assertGreater(native.st_ino,0)
        self.assertEqual(native.st_ino,os.lstat(path).st_ino)

    def test_ws_origin_confirmation_and_path_gate(self):
        import server
        manager=object.__new__(server.ManagerServer);manager.relay=self.controller;messages=[]
        async def send(ws,msg):messages.append(msg)
        async def blocking(fn,*args,**kwargs):return fn(*args,**kwargs)
        manager._send=send;manager._run_blocking=blocking
        manager._broadcast_devices=lambda:None;manager._broadcast_async=lambda msg:None
        async def exercise():
            for origin,extra in [('http://external.example',{'confirmed':True}),
                (f'http://127.0.0.1:{server._HTTP_PORT}',{}),
                (f'http://127.0.0.1:{server._HTTP_PORT}',{'confirmed':True})]:
                ws=SimpleNamespace(request=SimpleNamespace(headers={'Origin':origin}))
                await manager._handle_message(ws,{'type':'relay-clear-cache','id':9,'root':str(self.share),**extra})
        make_history(self.share)
        asyncio.run(exercise())
        self.assertEqual([message['success'] for message in messages],[False,False,True])


    def test_manual_batches_resume_instead_of_starving_later_directories(self):
        state = {}
        paths = [make_history(self.share, session=f'sess-page-{n:02}') for n in range(7)]
        results = []
        with patch.object(relay_cache, 'SCAN_LIMIT', 2), patch.object(relay_cache, 'SCAN_PAGE_SIZE', 2):
            for _ in range(20):
                result = self.clean(scan_state=state)
                results.append(result)
                if result['completed']:
                    break
        self.assertTrue(results[-1]['completed'], results)
        self.assertTrue(results[0]['limited'])
        self.assertFalse(results[0]['completed'])
        self.assertTrue(any(r['resumed'] for r in results[1:]))
        self.assertTrue(all(not p.exists() for p in paths))
        self.assertEqual(sum(r['removedFiles'] for r in results), 7)
        self.assertEqual(state, {})

    def test_small_result_bodies_are_not_opened(self):
        paths = [self.file(f'results/retired/{n:016x}.json', {'reqId':f'{n:016x}','status':'ok','stdout':'small'}) for n in range(120)]
        original_open = open
        opened = []
        def track(path, *args, **kwargs):
            if str(path).endswith('.json'):
                opened.append(str(path))
            return original_open(path, *args, **kwargs)
        with patch.object(relay_cache, 'open', track, create=True):
            result = self.clean()
        self.assertTrue(result['completed'])
        self.assertFalse(opened, opened)
        self.assertTrue(all(p.exists() for p in paths))

    def test_queued_file_becoming_recent_is_not_deleted_on_resume(self):
        state = {}
        first=make_history(self.share,session='sess-aa')
        second=make_history(self.share,session='sess-bb')
        with patch.object(relay_cache,'SCAN_LIMIT',1):
            initial=self.clean(scan_state=state)
        self.assertFalse(initial['completed'])
        self.assertFalse(first.exists())
        os.utime(second,(self.clock,self.clock))
        result=self.clean(scan_state=state)
        self.assertTrue(second.exists())
        self.assertTrue(result['completed'])

    def test_controller_marks_partial_and_resumes_only_after_explicit_call(self):
        for n in range(3):make_history(self.share,session=f'sess-part-{n}')
        with patch.object(relay_cache,'SCAN_LIMIT',1):
            first=self.controller.clear_cache(str(self.share),True)
        self.assertIn('部分完成',first['output'])
        self.assertFalse(first['completed'])
        second=self.controller.clear_cache(str(self.share),True)
        self.assertTrue(second['resumed'])
        self.assertTrue(second['completed'])
        self.assertIn('继续上次扫描',second['output'])


def run():
    return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CacheTests)).wasSuccessful()


if __name__=='__main__':sys.exit(0 if run() else 1)
