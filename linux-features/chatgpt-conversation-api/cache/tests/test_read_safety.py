import hashlib
import json
from pathlib import Path

import pytest

from chat_api_cache.cache import Cache
from chat_api_cache.server import Adapter, unpack
from chat_api_cache.transport import StdioClient, StdioProtocolError
from chat_api_cache.worker import run_job
from test_cache import CTX, setup, read, raw_fixture

RUNTIME=Path(__file__).resolve().parents[2]/'runtime'
TRANSPORT=RUNTIME/'cache/chat_api_cache/native_mcp.cjs'


def client_script(tmp_path, source, timeout=10):
    path=tmp_path/'upstream.cjs';path.write_text(source)
    client=StdioClient({'command':'node','args':[str(path)]},timeout=timeout)
    client.rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'synthetic','version':'1'}})
    client.send({'jsonrpc':'2.0','method':'notifications/initialized'})
    return client


@pytest.mark.skipif(not (RUNTIME/'ipc.js').is_file(),reason='Native runtime unavailable')
def test_large_reply_retains_exact_json_and_next_request_works(tmp_path):
    raw=raw_fixture()
    script=f'''const {{serve}}=require({json.dumps(str(TRANSPORT))});
const raw={json.dumps(raw)};
raw.pages[0].mapping.reply.message.content.parts=['x'.repeat(34*1024*1024)];
raw.pages[0].number=1e-7;raw.pages[0].surrogate='\\ud800';
serve({{runtime:{json.dumps(str(RUNTIME))},getClient:async()=>({{call:async()=>({{result:raw}}),close(){{}}}})}});
'''
    client=client_script(tmp_path,script,timeout=20)
    cache=Cache(tmp_path/'cache')
    try:
        result=client.tool('chatgpt_get_conversation',{})
        assert result['_meta']['hydex/structured-json-text'] is True
        assert len(result['content'][0]['text'])<200
        exact=result['_native_json_text'].encode()
        cache.record(CTX,'chat-one',result)
        hit=cache.lookup(CTX,'chat-one')
        assert hit['sha']==hashlib.sha256(exact).hexdigest()
        assert len(hit['data']['pages'][0]['mapping']['reply']['message']['content']['parts'][0])==34*1024*1024
        assert hit['data']['pages'][0]['surrogate']=='\ud800'
        assert client.rpc('ping',{})=={}
    finally:client.close();cache.close()


@pytest.mark.skipif(not (RUNTIME/'ipc.js').is_file(),reason='Native runtime unavailable')
def test_truly_oversized_result_has_bounded_error_without_poisoning_stream(tmp_path):
    script=f'''const {{serve}}=require({json.dumps(str(TRANSPORT))});
serve({{runtime:{json.dumps(str(RUNTIME))},frameLimit:4096,getClient:async()=>({{call:async()=>({{result:{{data:'x'.repeat(8192)}}}}),close(){{}}}})}});
'''
    client=client_script(tmp_path,script)
    try:
        result=client.tool('chatgpt_get_conversation',{})
        assert unpack(result)['error']['code']=='RESPONSE_TOO_LARGE'
        assert client.rpc('ping',{})=={}
    finally:client.close()


@pytest.mark.parametrize('mode',['timeout','parse_error'])
def test_broken_connection_is_closed_and_never_reused(tmp_path,mode):
    script=tmp_path/'broken.py'
    script.write_text('''import json,sys,time
for line in sys.stdin:
 r=json.loads(line)
 if 'id' not in r:continue
 if r['method']=='initialize':print(json.dumps({'id':r['id'],'result':{}}),flush=True)
 elif sys.argv[1]=='parse_error':print(json.dumps({'jsonrpc':'2.0','id':None,'error':{'code':-32700}}),flush=True)
 else:time.sleep(10)
''')
    import sys
    client=StdioClient({'command':sys.executable,'args':[str(script),mode]},timeout=.2)
    client.rpc('initialize',{})
    with pytest.raises(StdioProtocolError) as error:client.rpc('ping',{})
    assert error.value.code==('NATIVE_READ_TIMEOUT' if mode=='timeout' else 'UPSTREAM_PROTOCOL_ERROR')
    assert client.closed and client.process.poll() is not None
    with pytest.raises(StdioProtocolError,match='discarded'):client.rpc('ping',{})
    client.close()


def test_one_timeout_opens_persistent_circuit_and_next_chat_makes_no_call(setup):
    adapter,upstream,config=setup
    original=upstream.tool
    def failing(name,args):
        if name=='chatgpt_get_conversation':raise StdioProtocolError('NATIVE_READ_TIMEOUT','Synthetic timeout')
        return original(name,args)
    upstream.tool=failing
    assert unpack(read(adapter,mode='raw'))['error']['code']=='READ_CIRCUIT_OPEN'
    calls=len(upstream.calls)
    other=Adapter(config,upstream,launcher=lambda *_:None)
    try:
        result=other.tool('chatgpt_get_conversation',{'context':CTX,'id':'different-chat','cache_policy':'refresh'})
        assert unpack(result)['error']['code']=='READ_CIRCUIT_OPEN'
        assert len(upstream.calls)==calls
        assert other.cache.read_control(CTX)['error']['code']=='NATIVE_READ_TIMEOUT'
        assert other.cache.db.execute('SELECT count(*) FROM attempts').fetchone()[0]==0
    finally:other.cache.close()


def test_paused_workers_do_not_dispatch_and_do_not_retry_forever(setup):
    adapter,upstream,config=setup
    job=adapter.cache.queue_refresh(CTX,'body','chat-one')
    adapter.cache.suspend_reads()
    assert run_job(config,adapter.cache,upstream,job)['code']=='READS_PAUSED'
    assert upstream.calls==[]
    assert adapter.cache.refresh_job(CTX,'body','chat-one')['state']=='paused'
    assert adapter.cache.next_refresh('body') is None
    assert unpack(read(adapter,mode='raw',cache_policy='refresh'))['error']['code']=='READS_PAUSED'
    assert upstream.calls==[]


def test_server_failure_trips_circuit_without_marking_chat_unavailable(setup):
    adapter,upstream,_=setup
    upstream.error_status=503
    assert unpack(read(adapter,mode='raw'))['error']['code']=='READ_CIRCUIT_OPEN'
    assert adapter.cache.db.execute('SELECT http_status FROM attempts ORDER BY sequence DESC LIMIT 1').fetchone()[0]==503
    assert adapter.cache.db.execute("SELECT count(*) FROM index_events WHERE kind='unavailable'").fetchone()[0]==0


@pytest.mark.parametrize('status',[401,403])
def test_auth_failure_stops_reads_instead_of_walking_other_chats(setup,status):
    adapter,upstream,_=setup
    upstream.error_status=status
    assert unpack(read(adapter,mode='raw'))['error']['code']=='READ_CIRCUIT_OPEN'
    assert adapter.cache.read_control(CTX)['error']['code']=='AUTH_REQUIRED'
    assert upstream.reads==1
    read(adapter,mode='raw')
    assert upstream.reads==1


@pytest.mark.parametrize('resume',[False,True])
def test_explicit_probe_preserves_pause_unless_resumption_is_requested(tmp_path,monkeypatch,resume):
    import chat_api_cache.recovery as recovery
    from chat_api_cache.server import packed
    calls=[]
    class Probe:
        def rpc(self,*_):return {}
        def send(self,*_):pass
        def close(self):pass
        def tool(self,name,args):
            calls.append(name)
            if name=='chatgpt_get_inventory_status':return packed({'context':CTX,'status':'completed'})
            return packed({'connected':True,'context':CTX}) if name=='chatgpt_get_status' else packed(raw_fixture())
    monkeypatch.setattr(recovery,'StdioClient',lambda *_a,**_k:Probe())
    config={'directory':str(tmp_path/'raw'),'upstream':{}}
    cache=Cache(config['directory']);cache.suspend_reads();cache.close()
    result=recovery.probe(config,'chat-one',resume=resume)
    assert result['complete'] and result['resumed']==resume
    cache=Cache(config['directory'])
    try:
        assert (cache.read_control(CTX) is None)==resume
        assert cache.lookup(CTX,'chat-one')['data']['complete']
        assert calls.count('chatgpt_get_conversation')==1
    finally:cache.close()


def test_failed_probe_makes_one_attempt_and_keeps_circuit_open(tmp_path,monkeypatch):
    import chat_api_cache.recovery as recovery
    from chat_api_cache.server import packed
    calls=[]
    class Probe:
        def rpc(self,*_):return {}
        def send(self,*_):pass
        def close(self):pass
        def tool(self,name,args):
            if name=='chatgpt_get_status':return packed({'connected':True,'context':CTX})
            calls.append(name);raise StdioProtocolError('NATIVE_READ_TIMEOUT','Synthetic outage')
    monkeypatch.setattr(recovery,'StdioClient',lambda *_a,**_k:Probe())
    config={'directory':str(tmp_path/'raw'),'upstream':{}}
    result=recovery.probe(config,'chat-one',resume=True)
    cache=Cache(config['directory'])
    try:
        assert not result['complete'] and not result['resumed']
        assert cache.read_control(CTX)['state']=='open'
        assert len(calls)==1
    finally:cache.close()


def test_suspended_inventory_cancels_only_its_native_read_job(setup,monkeypatch):
    import chat_api_cache.worker as worker
    from chat_api_cache.server import packed
    adapter,upstream,config=setup
    upstream.metadata_polls=10
    original=upstream.tool;cancelled=[]
    def request(name,args):
        result=original(name,args)
        if name=='chatgpt_get_inventory_status':adapter.cache.suspend_reads()
        return result
    upstream.tool=request
    class Control:
        def rpc(self,*_):return {}
        def send(self,*_):pass
        def close(self):pass
        def tool(self,name,args):
            assert name=='chatgpt_cancel_job' and args['context']==CTX
            cancelled.append(args['job_id']);return packed({'status':'cancelled'})
    monkeypatch.setattr(worker,'StdioClient',lambda *_a,**_k:Control())
    monkeypatch.setattr(worker.time,'sleep',lambda *_:None)
    job=adapter.cache.queue_refresh(CTX,'inventory')
    assert worker.run_job(config,adapter.cache,upstream,job)['code']=='READS_PAUSED'
    assert cancelled==['inventory-one']
    assert adapter.cache.refresh_job(CTX,'inventory')['state']=='paused'


def test_persistent_adapter_reconnects_only_after_explicit_recovery_epoch(setup,monkeypatch):
    import chat_api_cache.server as server
    from test_cache import Upstream
    adapter,upstream,_=setup
    original=upstream.tool;upstream.closed=False
    def fail(name,args):
        if name=='chatgpt_get_conversation':
            upstream.closed=True
            raise StdioProtocolError('NATIVE_READ_TIMEOUT','Synthetic timeout')
        return original(name,args)
    upstream.tool=fail
    assert unpack(read(adapter,mode='raw'))['error']['code']=='READ_CIRCUIT_OPEN'
    opened=[]
    class Recovered(Upstream):
        def __init__(self):super().__init__();self.closed=False
        def rpc(self,method,params=None):return {} if method=='initialize' else super().rpc(method,params)
        def send(self,*_):pass
    def connect(*_a,**_k):
        fresh=Recovered();opened.append(fresh);return fresh
    monkeypatch.setattr(server,'StdioClient',connect)
    assert unpack(read(adapter,mode='raw'))['error']['code']=='READ_CIRCUIT_OPEN'
    assert opened==[]
    adapter.cache.resume_reads(CTX)
    assert not read(adapter,mode='raw')['isError']
    assert len(opened)==1 and opened[0].reads==1
