"""Versioned source admission for final-answer candidates, not answer validity."""

from agent_core.domain import TurnStatus


def candidate_turn_ready(config, turn):
    if turn is None:
        return False
    return turn.status is TurnStatus.COMPLETED or (
        config is not None and config.contract_version in {19, 20, 21}
        and turn.status is TurnStatus.RUNNING
    )
