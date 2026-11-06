"""The preview of a legacy request with a structured format shows the contract as the request gets it (no wrapper).

relay_texts.structured wraps the contract only in the head a profile composes; the legacy head appends the built-in one
(orchestrator._run_once), so the preview of the legacy path must too. A JSON-only answer is a reply the relay validates:
the preview used to show the user's wrapper there, a head the model never sees."""
import copy
import json

import pytest

from prompt_support import client_request, head_of, prompt_relay  # noqa: F401 - prompt_relay is a fixture

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
WRAPPER = "WRAPPED CONTRACT START\n{{default}}\nWRAPPED CONTRACT END"


def structured_body(system):
    body = client_request("chat", [{"role": "user", "content": "answer in json"}], system=system, tools=False)
    body["response_format"] = {"type": "json_schema", "json_schema": {"name": "r", "schema": SCHEMA}}
    return body


def start(prompt_relay, tmp_path, rules):
    doc = {"version": 1, "relay_texts": {"structured": WRAPPER},
           "profiles": {"wrapped": {"head": "{{client_system}}"}}, "rules": rules}
    (tmp_path / "prompts.json").write_text(json.dumps(doc), encoding="utf-8")
    return prompt_relay()


@pytest.mark.parametrize("system", ["CLIENT_SYSTEM", ""])
def test_a_legacy_request_previews_the_contract_it_is_sent_with(prompt_relay, tmp_path, system):
    relay = start(prompt_relay, tmp_path, [])  # no rule and no default_profile: the legacy path
    body = structured_body(system)
    preview = relay.runtime.prompts.preview(copy.deepcopy(body), "chat", {})
    relay.post("chat", body)
    sent = head_of(relay.posts[-1])
    assert preview["profile"] is None
    assert preview["head"] == sent
    assert "WRAPPED CONTRACT" not in sent and "WRAPPED CONTRACT" not in preview["head"]


def test_a_profile_still_wraps_the_contract_in_both(prompt_relay, tmp_path):
    relay = start(prompt_relay, tmp_path, [{"name": "all", "if": {}, "use": "wrapped"}])
    body = structured_body("CLIENT_SYSTEM")
    preview = relay.runtime.prompts.preview(copy.deepcopy(body), "chat", {})
    relay.post("chat", body)
    sent = head_of(relay.posts[-1])
    assert preview["profile"] == "wrapped" and preview["head"] == sent
    assert "WRAPPED CONTRACT START" in sent
