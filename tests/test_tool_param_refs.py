"""C6: a nested object written with $ref or allOf reaches the model as an object, and "number or string" stays both."""
import json

import pytest

import direct

ADDRESS = {"type": "object", "title": "Address", "description": "A postal address",
           "properties": {"street": {"type": "string"}, "zip": {"type": "string", "pattern": "^[0-9]{5}$"}},
           "required": ["street"]}
COLOR = {"type": "string", "enum": ["red", "blue"], "title": "Color"}


def tool(properties, defs=None, required=(), key="$defs", **root):
    parameters = {"type": "object", "properties": properties, "required": list(required), **root}
    if defs is not None:
        parameters[key] = defs
    return {"type": "function", "function": {"name": "t", "parameters": parameters}}


def params(*args, **kwargs):
    return direct.tools_to_retool([tool(*args, **kwargs)])[0]["params"]


def test_a_ref_to_defs_is_an_object_with_its_properties():
    got = params({"home": {"$ref": "#/$defs/Address"}, "work": {"$ref": "#/$defs/Address"}}, {"Address": ADDRESS},
                 required=["home"])
    expected = {"type": "object", "description": "A postal address", "properties": {
        "street": {"type": "string", "required": True},
        "zip": {"type": "string", "description": "Limits: matching the regular expression ^[0-9]{5}$.",
                "required": False}}}
    assert got["home"] == {**expected, "required": True}
    assert got["work"] == {**expected, "required": False}


def test_definitions_and_an_escaped_pointer_resolve_as_well():
    got = params({"a": {"$ref": "#/definitions/Address"}, "b": {"$ref": "#/definitions/a~1b"}},
                 {"Address": ADDRESS, "a/b": COLOR}, key="definitions")
    assert got["a"]["type"] == "object" and "street" in got["a"]["properties"]
    assert got["b"] == {"type": "enum", "enum": ["red", "blue"], "required": False}


def test_a_ref_to_an_enum_is_a_retool_enum_also_when_optional():
    got = params({"color": {"$ref": "#/$defs/Color"}, "tint": {"anyOf": [{"$ref": "#/$defs/Color"}, {"type": "null"}],
                                                                "default": None}}, {"Color": COLOR})
    assert got["color"] == {"type": "enum", "enum": ["red", "blue"], "required": False}
    assert got["tint"] == {"type": "enum", "enum": ["red", "blue"], "required": False}


def test_what_the_field_says_itself_wins_over_the_referenced_schema():
    got = params({"home": {"$ref": "#/$defs/Address", "description": "Where they live"}}, {"Address": ADDRESS})
    assert got["home"]["description"] == "Where they live"
    got = params({"home": {"allOf": [{"$ref": "#/$defs/Address"}], "description": "Where they live"}},
                 {"Address": ADDRESS})
    assert got["home"]["description"] == "Where they live" and got["home"]["type"] == "object"


def test_allof_members_are_merged_into_one_object():
    profile = {"allOf": [{"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
                         {"type": "object", "properties": {"age": {"type": "integer", "minimum": 0}}}],
               "description": "Who"}
    got = params({"profile": profile}, required=["profile"])["profile"]
    assert got == {"type": "object", "description": "Who", "required": True, "properties": {
        "name": {"type": "string", "required": True},
        "age": {"type": "number", "description": "Limits: >= 0.", "required": False}}}


def test_allof_with_a_ref_and_extra_keys_adds_them_up():
    schema = {"allOf": [{"$ref": "#/$defs/Address"}, {"properties": {"country": {"type": "string"}},
                                                      "required": ["country"]}]}
    got = params({"where": schema}, {"Address": ADDRESS})["where"]
    assert list(got["properties"]) == ["street", "zip", "country"]
    assert got["properties"]["street"]["required"] and got["properties"]["country"]["required"]
    assert not got["properties"]["zip"]["required"]


def test_a_property_that_two_allof_members_both_describe_is_merged_too():
    schema = {"allOf": [{"type": "object", "properties": {"n": {"type": "number", "minimum": 1}}},
                        {"type": "object", "properties": {"n": {"maximum": 9}}}]}
    assert params({"x": schema})["x"]["properties"]["n"] == {
        "type": "number", "description": "Limits: >= 1; <= 9.", "required": False}


def test_a_list_of_references_gives_items_that_are_objects():
    got = params({"places": {"type": "array", "items": {"$ref": "#/$defs/Address"}}}, {"Address": ADDRESS})["places"]
    assert got["type"] == "array" and got["items"]["type"] == "object" and "street" in got["items"]["properties"]


def test_the_root_of_the_parameters_may_be_a_reference():
    function = {"name": "t", "parameters": {"$ref": "#/definitions/Args", "definitions": {"Args": {
        "type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}}}}
    assert direct.tools_to_retool([{"type": "function", "function": function}])[0]["params"] == {
        "q": {"type": "string", "required": True}}


def test_a_schema_that_contains_itself_ends():
    node = {"type": "object", "properties": {"name": {"type": "string"}, "child": {"$ref": "#/$defs/Node"}}}
    got = params({"tree": {"$ref": "#/$defs/Node"}}, {"Node": node})["tree"]
    assert got["properties"]["name"]["type"] == "string"
    # the type is typed where it first appears; inside itself it is a plain object, so the schema is finite
    assert got["properties"]["child"] == {"type": "object", "properties": {}, "required": False}
    assert len(json.dumps(got)) < 3000
    # two types that point at each other, and a reference to the whole schema
    mutual = {"A": {"type": "object", "properties": {"b": {"$ref": "#/$defs/B"}}},
              "B": {"type": "object", "properties": {"a": {"$ref": "#/$defs/A"}}}}
    assert params({"x": {"$ref": "#/$defs/A"}}, mutual)["x"]["type"] == "object"
    assert params({"again": {"$ref": "#"}})["again"]["type"] == "object"


@pytest.mark.parametrize("ref", ["#/$defs/Missing", "https://example.invalid/schema.json", "#anchor", "#/$defs", "",
                                 7, None, "#/$defs/Address/properties/street/nope"])
def test_a_reference_that_points_nowhere_leaves_the_field_as_it_was(ref):
    got = params({"x": {"$ref": ref, "description": "kept"}}, {"Address": ADDRESS})["x"]
    assert got["type"] in ("string", "object", "enum") and got["description"] == "kept"


def test_a_number_or_string_field_stays_a_union_and_keeps_its_text():
    got = params({"version": {"type": ["number", "string"], "description": "Version"}})["version"]
    assert got == {"type": "anyOf", "description": "Version", "required": False,
                   "anyOf": [{"type": "number", "required": False}, {"type": "string", "required": False}]}
    tools = direct.tools_to_retool([tool({"version": {"type": ["number", "string"]}, "count": {"type": "number"}})])
    # "007" stays text in a union, a number field still gets its number
    assert json.loads(direct.coerce_arguments('{"version": "007", "count": "7"}', tools[0]["params"])) == {
        "version": "007", "count": 7}
    stream = direct.ArgStream(tools[0]["params"])
    assert stream.feed('{"version": "007", "count": "7"}') + stream.finish() == '{"version": "007", "count": 7}'


@pytest.mark.parametrize("kinds,expected", [
    (["number", "null"], "number"), (["integer", "null"], "number"), (["integer", "number"], "number"),
    (["string", "null"], "string"), (["null"], "null"), (["boolean", "null"], "boolean"),
    (["string", "array"], "anyOf"), (["integer", "string", "null"], "anyOf"), (["object", "array"], "anyOf")])
def test_only_a_list_of_several_real_types_is_a_union(kinds, expected):
    assert params({"x": {"type": kinds}})["x"]["type"] == expected


def test_a_type_list_with_an_enum_keeps_the_enum():
    got = params({"x": {"type": ["string", "number"], "enum": ["a", "b"]}})["x"]
    assert got == {"type": "enum", "enum": ["a", "b"], "required": False}


def test_the_branches_of_a_type_list_keep_their_own_keywords():
    got = params({"x": {"type": ["array", "string"], "items": {"type": "number"}, "maxLength": 5}})["x"]
    kinds = {branch["type"]: branch for branch in got["anyOf"]}
    assert kinds["array"]["items"] == {"type": "number", "required": False}
    assert kinds["string"]["description"] == "Limits: at most 5 characters."
