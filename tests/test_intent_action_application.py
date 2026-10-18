from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from agent_core.intent_action_application import IntentActionApplication
from agent_core.modeling import InspectFileAction, StopIntentAction


def test_action_application_fixes_inspection_ids() -> None:
    ids = [str(uuid4()) for _ in range(5)]
    claim = IntentActionApplication(
        ids[0],
        ids[1],
        "intent",
        "plan",
        InspectFileAction("/workspace/input/sample.txt"),
        tool_request_id=ids[2],
        retry_request_id=ids[3],
        complete_plan_mutation_id=ids[4],
    )
    assert claim.retry_request_id != claim.tool_request_id
    with pytest.raises(ValueError, match="distinct"):
        replace(claim, retry_request_id=claim.tool_request_id)


def test_stop_action_cannot_reserve_a_tool_request() -> None:
    with pytest.raises(ValueError, match="stop must not"):
        IntentActionApplication(
            str(uuid4()),
            str(uuid4()),
            "intent",
            "plan",
            StopIntentAction("done"),
            tool_request_id=str(uuid4()),
        )
