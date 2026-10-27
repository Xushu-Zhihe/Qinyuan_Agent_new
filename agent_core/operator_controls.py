"""Trusted operator admission and deadline commands; never model business decisions."""
import json
import math
import threading
from dataclasses import replace
from uuid import UUID


MIGRATION = """
CREATE TABLE task_operator_controls (
    operation_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    request_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX task_operator_controls_owner ON task_operator_controls(task_run_id);
"""


class OperatorPaused(Exception):
    def __init__(self):super().__init__("operator_paused")


def current(store, task_id):
    if getattr(store, "_schema_version", 0) < 31:return None
    row=store._connection.execute("SELECT state_json FROM task_operator_controls WHERE task_run_id=? ORDER BY rowid DESC LIMIT 1",(task_id,)).fetchone()
    return json.loads(row[0]) if row is not None else None


def check_admission(store, task_id):
    probe=getattr(store,"operator_pause_probe",None)
    if probe is not None and probe():raise OperatorPaused()
    state=current(store,task_id)
    if state is not None and state["paused"]:raise OperatorPaused()


def submit(store, task_id, operation_id, action, payload):
    """One serialized trusted transaction; commands do not invalidate in-flight Checkpoints."""
    try:identity=UUID(operation_id)
    except (ValueError,TypeError,AttributeError):raise ValueError("operator_operation_invalid") from None
    if str(identity)!=operation_id or identity.version!=4:raise ValueError("operator_operation_invalid")
    if not isinstance(payload,dict):raise ValueError("operator_payload_invalid")
    request=json.dumps(dict(action=action,payload=payload),sort_keys=True,separators=(",",":"),allow_nan=False)
    db=store._connection
    db.execute("BEGIN IMMEDIATE")
    try:
        prior=db.execute("SELECT task_run_id,request_json,state_json FROM task_operator_controls WHERE operation_id=?",(operation_id,)).fetchone()
        if prior is not None:
            if prior[0]!=task_id or prior[1]!=request:raise ValueError("operator_operation_conflict")
            db.commit();return json.loads(prior[2])
        task=store.load_task_run(task_id);config=store.load_d3_run_config(task_id)
        now=store._now()
        stage = store.load_stage_run_config(task_id)
        if task is None or stage is None and (config is None or config.contract_version!=22):raise ValueError("operator_scope_invalid")
        if task.status.value not in {"running","awaiting_external_feedback"} or now>=task.deadline_at:raise ValueError("operator_task_expired_or_terminal")
        if db.execute("SELECT 1 FROM d4_audit_events WHERE task_run_id=? AND category='cancellation_requested' LIMIT 1",(task_id,)).fetchone():raise ValueError("operator_task_cancelled")
        state=current(store,task_id)
        if action=="configure":
            if state is not None or set(payload)-{"overall_ceiling_at"}!={"question_seconds","overall_deadline_at","platform_deadline_at"}:raise ValueError("operator_configuration_invalid")
            seconds=payload["question_seconds"]
            if type(seconds) is not int or seconds<1:raise ValueError("operator_configuration_invalid")
            state={"paused":False,"question_deadline_at":task.started_at+seconds,
                   "overall_deadline_at":payload["overall_deadline_at"],"platform_deadline_at":payload["platform_deadline_at"],"overall_ceiling_at":payload.get("overall_ceiling_at")}
            for key in ("overall_deadline_at","platform_deadline_at","overall_ceiling_at"):
                value=state[key]
                if value is not None and (type(value) not in {int,float} or not math.isfinite(value) or value<=now):raise ValueError("operator_configuration_invalid")
        else:
            if state is None:raise ValueError("operator_not_configured")
            state=dict(state)
            if action in {"pause","resume"}:
                if payload:raise ValueError("operator_payload_invalid")
                if action=="resume" and store.blocking_d4_operations(task_id):raise ValueError("operator_unknown_operation")
                state["paused"]=action=="pause"
            elif action in {"extend_question","extend_overall"}:
                if set(payload)!={"seconds"} or type(payload["seconds"]) is not int or payload["seconds"]<1:raise ValueError("operator_extension_invalid")
                key="question_deadline_at" if action=="extend_question" else "overall_deadline_at"
                if state[key] is None:raise ValueError("operator_budget_not_configured")
                try:proposed=state[key]+payload["seconds"]
                except OverflowError:raise ValueError("operator_extension_invalid") from None
                if not math.isfinite(proposed):raise ValueError("operator_extension_invalid")
                if action=="extend_question":
                    caps=[state[k] for k in ("overall_deadline_at","platform_deadline_at") if state[k] is not None]
                    if caps and proposed>min(caps):raise ValueError("operator_extension_exceeds_ceiling")
                if action=="extend_overall" and state.get("overall_ceiling_at") is not None and proposed>state["overall_ceiling_at"]:raise ValueError("operator_extension_exceeds_ceiling")
                state[key]=proposed
            else:raise ValueError("operator_action_invalid")
        effective=min(v for k,v in state.items() if k.endswith("deadline_at") and v is not None)
        if effective<task.deadline_at or effective<=now:raise ValueError("operator_deadline_cannot_shorten")
        db.execute("UPDATE task_runs SET deadline_at=? WHERE task_run_id=?",(effective,task_id))
        db.execute("INSERT INTO task_operator_controls VALUES (?,?,?,?,?)",(operation_id,task_id,request,json.dumps(state,sort_keys=True,separators=(",",":")),now))
        db.commit()
        from qinyuan_core import get_logger
        get_logger("ctrl").info("operator.control_applied",run=task_id,operation=operation_id,action=action,category="operator_control",deadline_at=effective)
        return state
    except BaseException:
        db.rollback();raise


def enable(controller):
    controller._operator_thread=threading.get_ident()


def refresh(controller):
    if getattr(controller,"_operator_thread",None)!=threading.get_ident():return
    if controller._persistent_state is None:return
    task=controller._persistent_state.task_run
    row=controller._persistent.store._connection.execute("SELECT deadline_at FROM task_runs WHERE task_run_id=?",(task.task_run_id,)).fetchone()
    if row is not None and row[0]!=task.deadline_at:
        controller._persistent_snapshot=replace(controller._persistent_snapshot,deadline_at=row[0])
        controller._persistent_state=replace(controller._persistent_state,task_run=replace(task,deadline_at=row[0]))


async def paused_result(app):
    import asyncio
    supervisor=getattr(app,"supervision",None)
    if supervisor is not None and supervisor.pending is not None:
        while not supervisor.pending[1].is_set():await asyncio.sleep(0.1)
        try:
            result=supervisor.poll()
            if hasattr(result,"__await__"):await result
        except OperatorPaused:pass
    unknown=bool(app.store.blocking_d4_operations(app.state.task_run.task_run_id))
    return app.result("operator_pause_outcome_unknown" if unknown else "operator_paused",recovery_required=unknown)
