"""Item 22 (a4 F12): a Chat `file` part and a Responses `input_file` item reach the model as text (a text file) or as a clear
note (anything else), and the conversation keeps its native history.

The Retool route has no file block. The Anthropic document block was fixed on its own (test_documents_in_tool_results); a Chat
file part and a Responses input_file item were not: the model got 200 characters of JSON, called the tool again (7 of 8 loops)
or hunted for the file on the host through the relay's exec feature (60b91554, 6ed0b785), and the conversation became a flat
transcript for good (history none)."""
import base64
import json
import urllib.parse

import native_history
import validate
from test_client_contracts import CFG

PDF_B64 = base64.b64encode(b"%PDF-1.4 " + bytes(range(256)) * 20).decode()
PDF = "data:application/pdf;base64," + PDF_B64
TOOL = {"type": "function", "function": {"name": "get_media", "description": "d",
                                         "parameters": {"type": "object", "properties": {}}}}
RESPONSES_TOOL = {"type": "function", **TOOL["function"]}


def chat(part, place):
    if place == "user":
        messages = [{"role": "user", "content": [{"type": "text", "text": "q"}, part]}]
    else:
        messages = [{"role": "user", "content": "q"},
                    {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "t1", "type": "function", "function": {"name": "get_media", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "t1", "content": [{"type": "text", "text": "attached"}, part]}]
    return validate.parse_chat({"model": "gpt-6.1-sol", "messages": messages, "tools": [TOOL]}, CFG)


def responses(part, place):
    if place == "user":
        items = [{"role": "user", "content": [{"type": "input_text", "text": "q"}, part]}]
    else:
        items = [{"role": "user", "content": "q"},
                 {"type": "function_call", "call_id": "t1", "name": "get_media", "arguments": "{}"},
                 {"type": "function_call_output", "call_id": "t1",
                  "output": [{"type": "input_text", "text": "attached"}, part]}]
    return validate.parse_responses({"model": "gpt-6.1-sol", "input": items, "tools": [RESPONSES_TOOL]}, CFG)


def file_part(dialect, **fields):
    return {"type": "file", "file": fields} if dialect == "chat" else {"type": "input_file", **fields}


def test_a_file_in_every_place_keeps_the_native_history_and_never_shows_its_json():
    for dialect, parse in (("chat", chat), ("responses", responses)):
        for place in ("user", "tool"):
            parsed = parse(file_part(dialect, filename="report.pdf", file_data=PDF), place)
            assert parsed.native_messages is not None, (dialect, place)
            assert PDF_B64[:60] not in parsed.prompt and '"file_data"' not in parsed.prompt, (dialect, place)
            assert "[file report.pdf, application/pdf, 5 KB:" in parsed.prompt and "cannot be read through this route" in parsed.prompt
            assert len(parsed.message_fingerprints) == len(parsed.native_messages) or dialect == "responses"


def test_a_text_file_is_decoded_and_given_to_the_model():
    text = "name,score\nAda,36\nGrace,41\n"
    for data in ("data:text/csv;base64," + base64.b64encode(text.encode()).decode(),
                 "data:text/csv," + urllib.parse.quote(text)):
        assert native_history.readable_block(file_part("chat", filename="scores.csv", file_data=data)) == (
            "[file scores.csv, text/csv]\n" + text)
    data = "data:application/json;base64," + base64.b64encode(b'{"a": 1}').decode()
    assert native_history.readable_block(file_part("responses", file_data=data)) == '[file, application/json]\n{"a": 1}'
    parsed = chat(file_part("chat", filename="scores.csv", file_data=data), "tool")
    assert '{"a": 1}' in parsed.prompt and parsed.native_messages is not None


def test_a_file_that_is_not_text_or_cannot_be_decoded_is_a_note():
    for data in ("data:text/plain;base64,!!!not base64!!!",
                 "data:text/plain;base64," + base64.b64encode(b"\xff\xfe\x00bad").decode(),
                 "data:application/octet-stream;base64," + PDF_B64):
        note = native_history.readable_block(file_part("chat", filename="x.bin", file_data=data))
        assert note.startswith("[file x.bin, ") and "cannot be read through this route" in note
        assert ("KB" in note) == (len(data) > 4000)  # the size is named when there is one to name


def test_a_url_and_an_uploaded_id_are_notes_too():
    url = native_history.readable_block(file_part("responses", file_url="https://example.com/a.pdf", filename="a.pdf"))
    assert url.startswith("[file a.pdf at https://example.com/a.pdf: the content of this file cannot be read")
    uploaded = native_history.readable_block(file_part("chat", file_id="file-abc123"))
    assert uploaded.startswith("[file (uploaded id file-abc123): the content of this file cannot be read")
    assert native_history.readable_block(file_part("chat")).startswith("[file: the content of this file")


def test_the_note_says_what_the_model_must_not_do_and_what_it_may():
    inline = native_history.readable_block(file_part("chat", filename="report.pdf", file_data=PDF))
    # a file that came inside the request exists nowhere else: nothing to look for
    assert "not on disk, so no tool can find it" in inline and "Do not look for it" in inline
    assert "tell the user that it could not be read" in inline and "ask for its text" in inline
    document = native_history.readable_block({"type": "document", "title": "r.pdf", "source": {
        "type": "base64", "media_type": "application/pdf", "data": PDF_B64}})
    # one a local Read returned may be on disk: another tool of the client may try once, the same tool may not
    assert "Do not read it again with this tool" in document and "use that once" in document
    assert "not on disk" not in document


def test_every_note_is_one_bracketed_line_and_never_empty():
    blocks = [file_part("chat", filename="a b.pdf", file_data=PDF), file_part("responses", file_id="f"),
              {"type": "document", "source": {"type": "url", "url": "https://e.com/a.pdf"}},
              {"type": "document", "source": {"type": "base64", "data": PDF_B64}}]
    for block in blocks:
        note = native_history.readable_block(block)
        assert note.startswith("[") and note.endswith("]") and "\n" not in note
    assert native_history.readable_block({"type": "image"}) is None and native_history.readable_block(None) is None
    assert native_history.readable_block({"type": "text", "text": "x"}) is None


def test_a_conversation_with_a_file_in_a_tool_result_stays_native_for_the_next_turns():
    first = chat(file_part("chat", filename="report.pdf", file_data=PDF), "tool")
    again = validate.parse_chat({"model": "gpt-6.1-sol", "tools": [TOOL], "messages": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "get_media", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": [{"type": "text", "text": "attached"},
                                                           file_part("chat", filename="report.pdf", file_data=PDF)]},
        {"role": "assistant", "content": "I cannot read it."},
        {"role": "user", "content": "try the second page"}]}, CFG)
    # the earlier messages are the same in both requests: the history continues instead of starting over
    assert first.native_messages == again.native_messages[:len(first.native_messages)]
    assert json.dumps(first.native_messages).count("cannot be read through this route") == 1
