"""A rejected body saved for bisection holds no secret the journal knows, and keeps everything else whole."""
import json

import direct
import journal


def test_a_saved_body_masks_known_secrets_whole_and_keeps_the_rest(tmp_path):
    token = "eyJhbGciOiJIUzI1NiJ9." + "a" * 600 + ".sig"
    previous = journal.JOURNAL._cfg
    journal.JOURNAL._cfg = {"cookie_values": [token], "accounts": []}
    try:
        long_text = "x" * 9000
        payload = json.dumps({"messages": [{"role": "tool", "content": "config: " + token}, {"role": "user",
                                                                                            "content": long_text}]})
        stamp = direct._dump_rejected_body(payload.encode(), "upstream said: bad " + token,
                                           cfg={"upstream_error_dir": str(tmp_path)}, status=400, request_id="r1")
    finally:
        journal.JOURNAL._cfg = previous
    body = (tmp_path / ("%s-body.json" % stamp)).read_text(encoding="utf-8")
    error = (tmp_path / ("%s-error.txt" % stamp)).read_text(encoding="utf-8")
    assert token not in body and token not in error
    assert long_text in body  # whole: nothing cut at 4,000 characters
    assert json.loads(body)["messages"][1]["content"] == long_text
