import queue
import adapters,generation
from types import SimpleNamespace
from orchestrator import _FailoverOut

def test_internal_route_binding_does_not_disable_preoutput_failover():
 out=_FailoverOut(queue.Queue());out.put({'type':'route_bound','output_cap':16});assert not out.started

def test_bound_output_cap_applies_only_retool_and_resets_on_retry():
 p=SimpleNamespace(model='claude-opus-5-5',system='',prompt='',tools=[],show_thinking=False,wants_usage=True,
   entry={'upstream':'unified'},structured_output=None,stop_sequences=[],output_cap=16)
 a=adapters.for_dialect('chat',p)
 a.feed({'type':'route_bound','output_cap':16})
 a.feed({'type':'route_bound','output_cap':None})
 a.feed({'type':'text','delta':'x'*200})
 assert a.payload('stop')['choices'][0]['message']['content']=='x'*200
 assert generation.output_cap({'id':p.model,'upstream':'minara'},16) is None
 assert generation.output_cap({'id':p.model,'upstream':'retool'},16)==16
