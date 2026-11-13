"""Explicit local recovery; one bounded native body read, never a retry loop."""
from __future__ import annotations
import time
from .cache import Cache
from .server import unpack
from .transport import StdioClient, StdioProtocolError


def probe(config, identifier, *, resume=False):
    cache=Cache(config['directory']);upstream=None;context=None
    try:
        upstream=StdioClient(config['upstream'],timeout=45)
        upstream.rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},
            'clientInfo':{'name':'hydex-bounded-read-probe','version':'5'}})
        upstream.send({'jsonrpc':'2.0','method':'notifications/initialized'})
        status=unpack(upstream.tool('chatgpt_get_status',{}))
        if not status.get('connected') or not status.get('context'):
            raise StdioProtocolError('PROBE_UNAVAILABLE','Native account is unavailable')
        context=status['context']
        args={'context':context,'id':identifier,'mode':'raw','branch':'all'}
        hint=cache.read_context(context,identifier)
        if hint:args['read_context']=hint
        with cache.fetch_lock(context,identifier):
            started=time.time();version=cache.version(context,identifier)
            response=upstream.tool('chatgpt_get_conversation',args)
            data=unpack(response)
            cache.record(context,identifier,response,started_at=started,metadata_version=version)
            if response.get('isError') or not data.get('complete'):
                raise StdioProtocolError((data.get('error') or {}).get('code','PROBE_INCOMPLETE'),'The probe did not return a complete raw snapshot')
            current=unpack(upstream.tool('chatgpt_get_status',{}))
            if current.get('context')!=context:
                raise StdioProtocolError('CONTEXT_CHANGED','Native account changed during the probe')
            upstream.rpc('ping',{})
            if resume:
                inventory=unpack(upstream.tool('chatgpt_get_inventory_status',{'context':context}))
                if inventory.get('context')!=context or inventory.get('status') not in ('not_started','completed','cancelled','paused','interrupted','failed'):
                    raise StdioProtocolError('PROBE_INVENTORY_ACTIVE','A previous read job must stop before resuming')
            hit=cache.lookup(context,identifier)
        if resume:
            cache.resume_reads(context);cache.resume_reads()
            with cache.db:
                from .cache import scope
                cache.db.execute("UPDATE refresh_jobs SET state='queued',retry_at=NULL,finished_at=NULL WHERE scope=? AND state='paused'",(scope(context),))
        return {'complete':True,'pages':len(data['pages']),'snapshot_sha256':hit['sha'],
                'resumed':resume,'elapsed_seconds':round(time.time()-started,3)}
    except Exception as error:
        cache.suspend_reads(context,code=getattr(error,'code',None) or 'PROBE_FAILED',message='Bounded health probe failed; native reads remain stopped')
        return {'complete':False,'resumed':False,'error':getattr(error,'code',None) or type(error).__name__}
    finally:
        if upstream:upstream.close()
        cache.close()
