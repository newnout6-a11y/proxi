"""An image-only tool result keeps its label in the text bridge.

The text bridge writes "[tool result <name> id=<call id>]:" before what a tool returned, and that label is how the model
tells which call a result answers. A result that was nothing but an image (a screenshot, a rendered page) lost the label
along with its text, and the chunk was the bare image (ultracode audit of the relay, C17)."""
import pytest

import validate
from test_client_contracts import CFG, _body

IMAGE = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJ"
         "ggg==")
PICTURE = {"type": "image", "imageData": IMAGE.split(",", 1)[1], "mimeType": "image/png"}


def chat(content, **extra):
    messages = [{"role": "user", "content": "shot"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "screenshot", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": content, **extra}]
    return validate.parse_chat(_body("chat", messages=messages), CFG)


def responses(output):
    items = [{"role": "user", "content": "shot"},
             {"type": "function_call", "call_id": "c1", "name": "screenshot", "arguments": "{}"},
             {"type": "function_call_output", "call_id": "c1", "output": output}]
    return validate.parse_responses(_body("responses", input=items), CFG)


def test_a_chat_tool_result_of_only_an_image_keeps_its_label():
    chunk = chat([{"type": "image_url", "image_url": {"url": IMAGE}}]).message_chunks[-1]
    assert chunk == [{"type": "text", "text": "[tool result c1 id=c1]:"}, PICTURE]


def test_the_label_names_the_tool_when_the_message_does():
    chunk = chat([{"type": "image_url", "image_url": {"url": IMAGE}}], name="screenshot").message_chunks[-1]
    assert chunk[0] == {"type": "text", "text": "[tool result screenshot id=c1]:"} and chunk[1] == PICTURE


def test_a_responses_tool_output_of_only_an_image_keeps_its_label():
    chunk = responses([{"type": "input_image", "image_url": IMAGE}]).message_chunks[-1]
    assert chunk == [{"type": "text", "text": "[tool result c1 id=c1]:"}, PICTURE]


def test_a_result_of_text_and_an_image_and_a_result_of_text_are_as_they_were():
    both = chat([{"type": "text", "text": "ok"}, {"type": "image_url", "image_url": {"url": IMAGE}}])
    assert both.message_chunks[-1] == [{"type": "text", "text": "[tool result c1 id=c1]: ok"}, PICTURE]
    assert chat("ok").message_chunks[-1] == "[tool result c1 id=c1]: ok"


def test_the_prompt_of_the_text_bridge_names_the_call_the_image_answers():
    assert "[tool result c1 id=c1]:" in chat([{"type": "image_url", "image_url": {"url": IMAGE}}]).prompt
