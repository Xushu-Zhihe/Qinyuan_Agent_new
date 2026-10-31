"""Provider schema and validator must agree on closed tool arguments."""

from agent_core.output_contracts import shape_issues
from agent_core.stage_planning import _closed_schema


def test_closed_schema_matches_validator_and_names_unknown_fields():
    schema = _closed_schema(
        {"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]}
    )
    assert schema["additionalProperties"] is False

    issues = shape_issues({"script": "x", "extra": 1, "more": 2}, schema, "$")
    unknown = [issue for issue in issues if issue.code == "field_unknown"]
    assert len(unknown) == 1
    assert "extra" in unknown[0].path
    assert "more" in unknown[0].actual


def test_nested_object_schemas_are_closed_too():
    schema = _closed_schema(
        {
            "type": "object",
            "properties": {
                "inner": {"type": "object", "properties": {"value": {"type": "string"}}}
            },
        }
    )
    assert schema["additionalProperties"] is False
    assert schema["properties"]["inner"]["additionalProperties"] is False
