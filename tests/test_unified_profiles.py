"""Effective prompt-profile controls constrain actual unified routes."""
import json
import time
from pathlib import Path

import pytest
import models
from test_unified_clients import unified  # noqa: F401

@pytest.mark.parametrize('dialect', ['chat','responses','messages'])
def test_independent_actual_profile_binding(unified,dialect):
 rt=unified.runtime
 path=Path(rt.cfg['prompts_file'])
 path.write_text(json.dumps({'version':1,'profiles':{'low-profile':{'generation':{'effort':'low'}}},'default_profile':'low-profile'}),encoding='utf8')
 time.sleep(1.1);rt.prompts.maybe_reload(); assert rt.prompts.snapshot() is not None
 descriptor=rt.pool.backends[1].cfg['minara_models'][0]
 # Profile low accepted by public union; actual Minara high/low capable for first case.
 descriptor['reasoning_effort_levels']=['low','high']
 rt.cfg['_minara_models']=models.build_minara_catalog([b.cfg for b in rt.pool.backends])
 # Prefer Minara for deterministic binding.
 rt.pool.backends[0].cfg['enabled']=False
 from test_minara_mixed_clients import request
 endpoint,body=request(dialect,'claude-opus-5-5')
 if dialect=='chat':body['reasoning_effort']='high'
 elif dialect=='responses':body['reasoning']={'effort':'high'}
 else:body['output_config']={'effort':'high'}
 status,raw=unified.call(endpoint,body,'profile-case')
 assert status==200
 assert unified.observed[-1]['payload'].get('reasoning_effort')=='low'
 descriptor['reasoning_effort_levels']=['low']
 rt.cfg['_minara_models']=models.build_minara_catalog([b.cfg for b in rt.pool.backends])
 status,raw=unified.call(endpoint,body,'profile-low-only')
