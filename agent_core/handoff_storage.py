"""SQLite-owned D8-2 metadata and atomic admission helpers."""

import json
from dataclasses import asdict

from agent_core.handoff_contracts import D8Capacity, D8CapacityVersion, D8HandoffVersion, D8TreeRecord
from agent_core.conclusion_revision_contracts import D8RevisionRecord
from agent_core.session_contracts import D8DecisionRecord


def _turn_authorized(config, binding):
    from agent_core.http_validation import valid_turn
    if getattr(binding, 'kind', None) in {'http_origin','hybrid_origin'}:
        return valid_turn(config, binding)
    return binding.attachment_index in config.authorization.attachment_indices

D8_HANDOFF_SCHEMA = """
CREATE TABLE d8_handoff_versions (
 record_id TEXT PRIMARY KEY, task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
 turn_id TEXT NOT NULL REFERENCES turns(turn_id), global_version INTEGER NOT NULL,
 turn_version INTEGER NOT NULL, metadata_json TEXT NOT NULL, event_sequence INTEGER NOT NULL,
 UNIQUE(task_run_id, global_version), UNIQUE(turn_id, turn_version));
CREATE TABLE d8_capacity_versions (
 record_id TEXT PRIMARY KEY, task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
 version INTEGER NOT NULL, metadata_json TEXT NOT NULL, event_sequence INTEGER NOT NULL,
 UNIQUE(task_run_id, version));
CREATE TABLE d8_tree_records (
 record_id TEXT PRIMARY KEY, task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
 turn_id TEXT, intent_id TEXT, kind TEXT NOT NULL,
 metadata_json TEXT NOT NULL, event_sequence INTEGER NOT NULL);
CREATE TABLE d8_history_index (
 task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id), ref TEXT NOT NULL,
 record_id TEXT NOT NULL, record_type TEXT NOT NULL, turn_id TEXT, intent_id TEXT,
 artifact_version_id TEXT, snapshot_ref TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
 event_sequence INTEGER NOT NULL, PRIMARY KEY(task_run_id, ref, event_sequence));
CREATE TABLE d8_history_reads (
 request_id TEXT PRIMARY KEY, task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
 start_json TEXT NOT NULL, completion_json TEXT);
"""

D8_REVISION_SCHEMA = """
CREATE TABLE d8_conclusion_revisions (
 record_id TEXT PRIMARY KEY, task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
 sequence INTEGER NOT NULL, metadata_json TEXT NOT NULL, event_sequence INTEGER NOT NULL,
 UNIQUE(task_run_id, sequence));
"""

D8_SESSION_SCHEMA = """
CREATE TABLE d8_decisions (
 record_id TEXT PRIMARY KEY, task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
 intent_id TEXT NOT NULL REFERENCES intents(intent_id), ordinal INTEGER NOT NULL,
 root_request_id TEXT NOT NULL, metadata_json TEXT NOT NULL, event_sequence INTEGER NOT NULL,
 UNIQUE(intent_id, ordinal), UNIQUE(task_run_id, root_request_id));
"""

D8_CHANGES = (D8HandoffVersion, D8CapacityVersion, D8TreeRecord, D8RevisionRecord, D8DecisionRecord)
D8_OPERATIONS = {"append_d8_handoff", "append_d8_capacity", "register_d8_tree_turn",
                 "select_d8_tree_intent", "evaluate_d8_tree_intent", "exhaust_d8_expansion",
                 "exhaust_d8_tree_turn", "append_d8_revision", "append_d8_decision"}


def load_records(store, table, task_id):
    minimum = 26 if table == "d8_decisions" else 25 if table == "d8_conclusion_revisions" else 24
    if store._schema_version < minimum:
        return ()
    rows = store._connection.execute(
        f"SELECT metadata_json FROM {table} WHERE task_run_id=? ORDER BY event_sequence", (task_id,))
    classes = {"d8_handoff_versions": D8HandoffVersion, "d8_capacity_versions": D8CapacityVersion,
               "d8_tree_records": D8TreeRecord, "d8_conclusion_revisions": D8RevisionRecord,
               "d8_decisions": D8DecisionRecord}
    result = []
    for row in rows:
        value = json.loads(row[0])
        if table == "d8_capacity_versions":
            value["capacity"] = D8Capacity(**value["capacity"])
        if table == "d8_handoff_versions":
            value["index_entries"] = tuple(tuple(i) for i in value["index_entries"])
        if table == "d8_tree_records":
            value["dependencies"] = tuple((i, tuple(refs)) for i, refs in value["dependencies"])
        item = classes[table](**value)
        if item.task_run_id != task_id:
            raise ValueError("d8_metadata_owner_changed")
        result.append(item)
    return tuple(result)


def validate_handoff_mutation(store, mutation):
    from agent_core.control_store import StorageError, UpsertTurn, UpsertIntent
    from agent_core.domain import IntentStatus, TurnStatus
    records = [c for c in mutation.state_changes if isinstance(c, D8_CHANGES)]
    if mutation.operation not in D8_OPERATIONS:
        if records:
            raise StorageError("D8-2 metadata requires its dedicated operation")
        return False
    config = store.load_d3_run_config(mutation.task_run_id)
    if config is None or config.contract_version not in {3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21} or len(records) != 1:
        raise StorageError("D8-2 requires its explicit internal contract")
    record = records[0]
    if record.task_run_id != mutation.task_run_id:
        raise StorageError("D8-2 cross-task metadata rejected")
    if isinstance(record, D8DecisionRecord):
        prior = [i for i in store.list_d8_decisions(record.task_run_id) if i.intent_id == record.intent_id]
        rows = store.load_control_state_rows(record.task_run_id)
        owner = next((i for i in rows.intents if i.intent_id == record.intent_id), None)
        if (config.contract_version not in {5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21} or mutation.operation != "append_d8_decision"
                or len(mutation.state_changes) != 1 or owner is None or owner.turn_id != record.turn_id
                or record.ordinal != len(prior) + 1
                or any(i.root_request_id == record.root_request_id for i in prior)):
            raise StorageError("D8-4 decision ownership or append relation changed")
        return True
    if any(not isinstance(c, (UpsertTurn, UpsertIntent, *D8_CHANGES)) for c in mutation.state_changes):
        raise StorageError("D8-2 transition cannot change other control objects")
    if isinstance(record, D8RevisionRecord):
        if (config.contract_version not in {4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21} or mutation.operation != "append_d8_revision"
                or len(mutation.state_changes) != 1
                or record.sequence != len(store.list_d8_revisions(record.task_run_id)) + 1):
            raise StorageError("D8-3 revision requires its append-only contract")
        owner = store._task_run_for_intent(record.actor_intent_id)
        if owner != record.task_run_id:
            raise StorageError("D8-3 actor belongs to another TaskRun")
        for identity, category in ((record.proposal_request_id, "action"),
                                   (record.review_request_id, "fact_review")):
            call = store.load_d4_operation(identity)
            if (call is None or call.start.task_run_id != record.task_run_id
                    or call.start.category != category or call.completion is None
                    or call.completion.result_kind != "completed" or call.application_ref is not None):
                raise StorageError("D8-3 revision requires known unapplied model results")
        return True
    if mutation.operation == "append_d8_revision":
        raise StorageError("D8-3 revision operation requires revision metadata")
    before = store.load_control_state_rows(record.task_run_id)
    for change in mutation.state_changes:
        if isinstance(change, UpsertIntent):
            old = next((i for i in before.intents if i.intent_id == change.intent_id), None)
            if old is not None and (old.turn_id, old.description, old.priority, old.intent_type) != (
                    change.turn_id, change.description, change.priority, change.intent_type):
                raise StorageError("D8-2 cannot rewrite an accepted Intent declaration")
        if isinstance(change, UpsertTurn):
            old = next((t for t in before.turns if t.turn_id == change.turn_id), None)
            if old is not None and (old.task_run_id, old.sequence, old.turn_target, old.intent_ids) != (
                    change.task_run_id, change.sequence, change.turn_target, change.intent_ids):
                raise StorageError("D8-2 cannot rewrite a registered Turn declaration")
    if isinstance(record, D8CapacityVersion):
        versions = store.list_d8_capacities(record.task_run_id)
        if (mutation.operation != "append_d8_capacity" or len(mutation.state_changes) != 1
                or record.version != len(versions) + 1):
            raise StorageError("D8-2 capacity version is not append-only")
        previous = versions[-1].capacity if versions else config.handoff_config.capacity
        if not versions:
            if record.capacity != previous:
                raise StorageError("initial D8-2 capacity differs from frozen configuration")
        else:
            updates = {k: v for k, v in asdict(record.capacity).items() if v != getattr(previous, k)}
            if previous.increased(updates) != record.capacity:
                raise StorageError("invalid D8-2 capacity increase")
        return True
    if isinstance(record, D8HandoffVersion):
        versions = store.list_d8_handoffs(record.task_run_id)
        if (mutation.operation != "append_d8_handoff" or len(mutation.state_changes) != 1
                or record.global_version != len(versions) + 1
                or record.turn_version != sum(v.turn_id == record.turn_id for v in versions) + 1
                or record.base_checkpoint_sequence != mutation.base_checkpoint_sequence
                or store._task_run_for_turn(record.turn_id) != record.task_run_id
                or record.capacity_version > len(store.list_d8_capacities(record.task_run_id))):
            raise StorageError("D8-2 handoff version binding changed")
        if record.model_request_id is not None:
            call = store.load_d4_operation(record.model_request_id)
            if (call is None or call.start.category != "handoff_summary"
                    or call.start.task_run_id != record.task_run_id or call.completion is None
                    or call.application_ref is not None):
                raise StorageError("D8-2 handoff requires an unapplied known call")
        return True
    turns = [c for c in mutation.state_changes if isinstance(c, UpsertTurn)]
    intents = [c for c in mutation.state_changes if isinstance(c, UpsertIntent)]
    expected_kind = {"register_d8_tree_turn": "registration", "select_d8_tree_intent": "selection",
                     "exhaust_d8_expansion": "expansion_exhausted", "exhaust_d8_tree_turn": "turn_exhausted"}
    if mutation.operation in expected_kind and record.kind != expected_kind[mutation.operation]:
        raise StorageError("D8-2 tree operation differs from its metadata")
    if mutation.operation in {"register_d8_tree_turn", "exhaust_d8_expansion", "evaluate_d8_tree_intent"} and not record.model_request_id:
        raise StorageError("D8-2 model decision requires its known model result")
    if record.model_request_id:
        call = store.load_d4_operation(record.model_request_id)
        expected = "review" if mutation.operation == "evaluate_d8_tree_intent" else "planning"
        if (call is None or call.start.task_run_id != record.task_run_id
                or call.start.category != expected or call.completion is None
                or call.completion.result_kind != "completed" or call.application_ref is not None):
            raise StorageError("D8-2 tree requires an unapplied known model result")
    if mutation.operation == "register_d8_tree_turn":
        metadata = mutation.d3
        if (len(turns) != 1 or turns[0].turn_id != record.turn_id
                or turns[0].status is not TurnStatus.PENDING or not 1 <= len(intents) <= 2
                or tuple(i.intent_id for i in intents) != turns[0].intent_ids
                or any(i.status is not IntentStatus.PENDING or i.turn_id != record.turn_id for i in intents)
                or metadata is None or metadata.turn_binding is None
                or metadata.turn_binding.turn_id != record.turn_id
                or metadata.turn_binding.proposal_request_id != record.model_request_id
                or metadata.turn_binding.task_run_id != record.task_run_id
                or not _turn_authorized(config, metadata.turn_binding)
                or {i.intent_id for i in metadata.intent_bindings} != set(turns[0].intent_ids)
                or any(i.task_run_id != record.task_run_id or i.turn_id != record.turn_id
                       or i.work_directory != f"/workspace/work/intents/{i.intent_id}" for i in metadata.intent_bindings)
                or tuple(i for i, _ in record.dependencies) != turns[0].intent_ids):
            raise StorageError("D8-2 tree registration binding changed")
        if turns[0].parent_turn_id is None and before.turns:
            raise StorageError("D8-2 cannot register another root Turn")
    elif mutation.operation == "select_d8_tree_intent":
        old = store.load_control_state_rows(record.task_run_id)
        item = next((i for i in old.intents if i.intent_id == record.intent_id), None)
        if (item is None or item.turn_id != record.turn_id or item.status is not IntentStatus.PENDING
                or len(intents) != 1 or intents[0].intent_id != item.intent_id
                or intents[0].status is not IntentStatus.RUNNING
                or any(i.status is IntentStatus.RUNNING for i in old.intents)
                or item.priority != min(i.priority for i in old.intents if i.turn_id == record.turn_id and i.status is IntentStatus.PENDING)
                or len(turns) != 1 or turns[0].turn_id != record.turn_id or turns[0].status is not TurnStatus.RUNNING):
            raise StorageError("D8-2 selection is not a pending sibling")
        active = [t for t in old.turns if t.status in {TurnStatus.RUNNING, TurnStatus.PENDING}]
        if not active or max(active, key=lambda t: t.sequence).turn_id != record.turn_id:
            raise StorageError("D8-2 must dispatch the active leaf before parent siblings")
    elif mutation.operation == "evaluate_d8_tree_intent":
        if record.kind not in {"succeeded", "failed", "inconclusive"} or len(intents) != 1:
            raise StorageError("D8-2 evaluation has no exact Intent transition")
        before = store.load_control_state_rows(record.task_run_id)
        old = next((i for i in before.intents if i.intent_id == record.intent_id), None)
        if (old is None or old.status is not IntentStatus.RUNNING
                or intents[0].intent_id != record.intent_id or intents[0].status.value != record.kind
                or record.support_fact_id is None or record.inner_request_id is None):
            raise StorageError("D8-2 evaluation binding changed")
        if turns:
            raise StorageError("D8-2 Intent evaluation cannot complete a Turn")
    elif mutation.operation == "exhaust_d8_tree_turn":
        rows = store.load_control_state_rows(record.task_run_id)
        if (len(turns) != 1 or turns[0].turn_id != record.turn_id
                or turns[0].status is not TurnStatus.COMPLETED
                or any(i.status in {IntentStatus.RUNNING, IntentStatus.PENDING}
                       for i in rows.intents if i.turn_id == record.turn_id)
                or any(t.parent_turn_id == record.turn_id and t.status is not TurnStatus.COMPLETED for t in rows.turns)):
            raise StorageError("D8-2 Turn is not exhausted")
    elif mutation.operation == "exhaust_d8_expansion" and (turns or intents):
        raise StorageError("D8-2 exhausted expansion cannot modify Intent state")
    return True


def save_handoff_mutation(store, mutation, sequence):
    for record in mutation.state_changes:
        metadata = json.dumps(asdict(record), sort_keys=True, ensure_ascii=False, separators=(",", ":")) if isinstance(record, D8_CHANGES) else None
        if isinstance(record, D8CapacityVersion):
            store._connection.execute("INSERT INTO d8_capacity_versions VALUES (?,?,?,?,?)",
                                      (record.record_id, record.task_run_id, record.version, metadata, sequence))
        elif isinstance(record, D8HandoffVersion):
            store._connection.execute("INSERT INTO d8_handoff_versions VALUES (?,?,?,?,?,?,?)",
                                      (record.record_id, record.task_run_id, record.turn_id, record.global_version,
                                       record.turn_version, metadata, sequence))
            for ref, record_id, record_type, turn_id, intent_id, artifact_id in record.index_entries:
                store._connection.execute("INSERT INTO d8_history_index VALUES (?,?,?,?,?,?,?,?,?,?)",
                                          (record.task_run_id, ref, record_id, record_type, turn_id, intent_id,
                                           artifact_id, record.evidence_ref, record.evidence_sha256, sequence))
            if record.model_request_id and record.origin == "model_organized":
                store._mark_d4_operation_applied(record.model_request_id, record.record_id)
        elif isinstance(record, D8TreeRecord):
            store._connection.execute("INSERT INTO d8_tree_records VALUES (?,?,?,?,?,?,?)",
                                      (record.record_id, record.task_run_id, record.turn_id, record.intent_id,
                                       record.kind, metadata, sequence))
            for request_id in (record.model_request_id, record.inner_request_id):
                if request_id:
                    store._mark_d4_operation_applied(request_id, record.record_id)
                    if store.load_d3_operation(request_id) is not None:
                        store._connection.execute("UPDATE d3_operations SET application_ref=? WHERE operation_id=?",
                                                  (record.record_id, request_id))
        elif isinstance(record, D8RevisionRecord):
            store._connection.execute("INSERT INTO d8_conclusion_revisions VALUES (?,?,?,?,?)",
                                      (record.record_id, record.task_run_id, record.sequence, metadata, sequence))
            for request_id in (record.proposal_request_id, record.review_request_id):
                store._mark_d4_operation_applied(request_id, record.record_id)
                if store.load_d3_operation(request_id) is not None:
                    store._connection.execute("UPDATE d3_operations SET application_ref=? WHERE operation_id=?",
                                              (record.record_id, request_id))
        elif isinstance(record, D8DecisionRecord):
            store._connection.execute("INSERT INTO d8_decisions VALUES (?,?,?,?,?,?,?)",
                                      (record.record_id, record.task_run_id, record.intent_id,
                                       record.ordinal, record.root_request_id, metadata, sequence))
