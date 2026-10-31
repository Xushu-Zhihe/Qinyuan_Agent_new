"""Serial competition orchestration. Agent decisions and TaskRun state stay in the CLI."""
import json
import math
import os
import threading
import time
from uuid import UUID,uuid4
from agent_adapters.competition import read_question,platform_action,_target_origin,_matches_target,supports_action
from agent_adapters.workbench_contracts import ACTIVE_RUN_STATUSES,WorkbenchError
from qinyuan_core import get_logger

PLATFORM_POLL_SECONDS=5.0
FINISHED={"achieved","failed","deadline_exceeded","cancelled","inconclusive"}
# A live target-bearing question may arrive before the platform publishes its address.
ADDRESS_WAIT_CATEGORIES=("web","pwn")


def _address_wait_seconds():
    """Operator-tunable wait window; invalid or out-of-range values keep 30 seconds."""
    try:value=float(os.environ.get("QINYUAN_COMPETITION_ADDRESS_WAIT_SECONDS",""))
    except (TypeError,ValueError):return 30.0
    return value if math.isfinite(value) and 0<=value<=900 else 30.0


ADDRESS_WAIT_SECONDS=_address_wait_seconds()


def _default_description(value):
    """Session-level note appended to every challenge description; empty disables it."""
    if value is None:return ""
    if not isinstance(value,str) or len(value)>16000:raise WorkbenchError("competition_session_invalid")
    return value.strip()


def _should_wait_address(item,adapter):
    """Poll for an address only while a live web/pwn question still has none; never require one."""
    if not isinstance(item,dict) or item.get("addresses"):return False
    remaining=item.get("remaining_seconds")
    if remaining is not None and remaining<=0:return False
    if item.get("state")!=("available" if adapter=="benchmark" else "active"):return False
    category=item.get("category") or ""
    if not isinstance(category,str):return False
    # Benchmark items carry no platform type; a container reported available
    # without an address is still waiting for its port.
    return adapter=="benchmark" if not category.strip() else any(
        name in category.strip().lower() for name in ADDRESS_WAIT_CATEGORIES)


class CompetitionSchedules:
    def __init__(self,workbench,*,clock=time.time):
        self.workbench=workbench;self.clock=clock
        self.wake=threading.Event();self.closed=threading.Event();self.tick_lock=threading.Lock()
        self.thread=None;self.automatic=True;self.commands_lock=threading.Lock()
        restarted=[]
        with workbench._db() as db:
            db.executescript("""
CREATE TABLE IF NOT EXISTS competition_sessions (
 id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES competition_profiles(id),
 config_json TEXT NOT NULL,state_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS competition_platform_operations (
 operation_id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES competition_sessions(id),
 slot INTEGER NOT NULL,action TEXT NOT NULL,remote_ref TEXT NOT NULL,
 receipt_json TEXT,created_at REAL NOT NULL);
""")
            for row in db.execute("SELECT id,state_json FROM competition_sessions").fetchall():
                state=json.loads(row["state_json"])
                if state["status"] in {"running","pausing"}:
                    state.update(status="paused",reason="competition_workbench_restarted")
                    db.execute("UPDATE competition_sessions SET state_json=? WHERE id=?",(json.dumps(state),row["id"]))
                    restarted.append((row["id"],state.get("challenge_id")))
        for identity,challenge in restarted:
            if challenge is not None and workbench.runner.status(challenge) is not None:
                try:workbench.runner.operator_control(challenge,"pause")
                except WorkbenchError as error:self.update(identity,status="needs_attention",reason=error.code)
        # A finished session must not keep its last question paused, and a deleted
        # question must not stay referenced: both block every following session.
        for row in self.rows():
            challenge=row["state"].get("challenge_id")
            if challenge is None:continue
            try:workbench.get(challenge)
            except WorkbenchError as error:
                if error.code!="challenge_not_found":raise
                self.update(row["id"],challenge_id=None)
                continue
            if row["state"]["status"]=="finished":workbench.release(challenge)


    def rows(self):
        with self.workbench._db() as db:rows=db.execute("SELECT * FROM competition_sessions ORDER BY rowid DESC").fetchall()
        return [dict(id=r["id"],config=json.loads(r["config_json"]),state=json.loads(r["state_json"])) for r in rows]

    def get(self,identity):
        row=next((r for r in self.rows() if r["id"]==identity),None)
        if row is None:raise WorkbenchError("competition_session_not_found",404)
        return row

    def project(self,row):
        result=dict(row);challenge=row["state"]["challenge_id"]
        if challenge is not None:
            try:result["runtime"]=self.workbench.runner.runtime_control(challenge)
            except WorkbenchError as error:
                result["runtime"]=None
                if error.code!="challenge_not_found":result["runtime_error"]="operator_state_read_failed"
            except (ValueError,OSError):result["runtime_error"]="operator_state_read_failed"
        return result

    def has_active(self):return any(r["state"]["status"]!="finished" for r in self.rows())
    def uses_profile(self,identity):return any(r["config"]["profile"]["id"]==identity and r["state"]["status"]!="finished" for r in self.rows())
    def owns(self,challenge):return any(r["state"].get("challenge_id")==challenge and r["state"]["status"]!="finished" for r in self.rows())

    def check_task_operation(self,challenge,operation):
        row=next((r for r in self.rows() if r["state"].get("challenge_id")==challenge and r["state"]["status"]!="finished"),None)
        if row is None or operation=="competition-end":return
        state=row["state"]
        if self.closed.is_set() or state["status"]!="running" or self.clock()>=state["deadline_at"]:
            # The session cannot make progress until the operator repairs this
            # question, and the session resume path re-raises the same recovery,
            # so an explicit per-task resume must stay reachable here.
            if not (operation=="resume" and state["status"]=="needs_attention"
                    and state.get("reason")=="competition_task_recovery_required"):
                raise WorkbenchError("competition_session_not_running",409)

    def update(self,identity,**changes):
        with self.workbench.lock,self.workbench._db() as db:
            state=self.get(identity)["state"];state.update(changes)
            db.execute("UPDATE competition_sessions SET state_json=? WHERE id=?",(json.dumps(state),identity))
        return state

    def start(self,value):
        from agent_adapters.workbench import http_methods
        required={"profile_id","default_scheme","question_minutes","overall_minutes","queue","http_methods","mode"}
        if not isinstance(value,dict) or set(value)-{"platform_deadline_at","default_description","default_description_enabled"}!=required:raise WorkbenchError("competition_session_invalid")
        if self.workbench.protocol_version!=3 or self.workbench.execution_profile!="d8_v22":raise WorkbenchError("competition_requires_contract22",409)
        if self.workbench.d7_mode=="simulated":raise WorkbenchError("mode_configuration_mismatch",409)
        if value["mode"]!="real" or value["default_scheme"] not in {"http","https"}:raise WorkbenchError("competition_session_invalid")
        if any(type(value[k]) is not int or value[k]<1 for k in ("question_minutes","overall_minutes")) or value["question_minutes"]>7*24*60:raise WorkbenchError("competition_session_invalid")
        methods=http_methods(value["http_methods"])
        profile=self.workbench.competitions._get(value["profile_id"])
        self.workbench.competitions.credential_for(profile)
        queue=value["queue"]
        if not isinstance(queue,list) or any(not isinstance(ref,str) or not ref for ref in queue) or len(set(queue))!=len(queue):raise WorkbenchError("competition_queue_invalid")
        if profile["adapter"]=="benchmark" and not queue or profile["adapter"]=="arena" and queue:raise WorkbenchError("competition_queue_invalid")
        try:end=self.clock()+value["overall_minutes"]*60
        except OverflowError:raise WorkbenchError("competition_session_invalid") from None
        if not math.isfinite(end):raise WorkbenchError("competition_session_invalid")
        ceiling=value.get("platform_deadline_at")
        if ceiling is not None and (type(ceiling) not in {int,float} or not math.isfinite(ceiling) or not self.clock()<ceiling or end>ceiling):raise WorkbenchError("competition_extension_exceeds_platform")
        config=dict(profile=profile,queue=queue,default_scheme=value["default_scheme"],mode=value["mode"],
                    question_seconds=value["question_minutes"]*60,http_methods=methods,platform_deadline_at=ceiling,
                    default_description=_default_description(value.get("default_description")),
                    default_description_enabled=bool(value.get("default_description_enabled")))
        state=dict(status="running",reason=None,index=0,current_ref=None,challenge_id=None,deadline_at=end,
                   stop_after_current=False,last_platform_check=0,platform=None,retry_operation=None)
        identity=str(uuid4())
        with self.workbench.lock,self.workbench._db() as db:
            if self.has_active() or self.workbench.runner._active:raise WorkbenchError("competition_schedule_active",409)
            self.workbench.competitions.credential_for(profile)
            db.execute("INSERT INTO competition_sessions VALUES (?,?,?,?)",(identity,profile["id"],json.dumps(config),json.dumps(state)))
        get_logger("ctrl").info("competition.session_started",session=identity,profile=profile["id"],category="schedule_control")
        self.launch();return self.get(identity)

    def launch(self):
        self.wake.set()
        if self.automatic and (self.thread is None or not self.thread.is_alive()):
            self.thread=threading.Thread(target=self.loop,daemon=True,name="competition-schedule");self.thread.start()

    def loop(self):
        while not self.closed.is_set():
            try:self.tick()
            except Exception as error:
                get_logger("ctrl").error("competition.scheduler_failed",category="schedule_control",exception_type=type(error).__name__)
                for row in self.rows():
                    if row["state"]["status"]=="running":self.pause(row["id"],reason="competition_scheduler_failed")
            self.wake.wait(1);self.wake.clear()

    def live(self,identity):
        row=self.get(identity)
        if self.closed.is_set() or row["state"]["status"]!="running":raise WorkbenchError("competition_session_not_running",409)
        if self.clock()>=row["state"]["deadline_at"]:raise WorkbenchError("competition_overall_deadline",409)
        self.workbench.competitions.credential_for(row["config"]["profile"])
        return row

    def pause(self,identity,*,reason="operator_paused"):
        row=self.get(identity)
        if row["state"]["status"]=="finished":return row
        self.update(identity,status="pausing",reason=reason)
        challenge=row["state"]["challenge_id"]
        if challenge is not None and self.workbench.runner.status(challenge) is not None:
            try:self.workbench.runner.operator_control(challenge,"pause")
            except WorkbenchError as error:
                self.update(identity,status="needs_attention",reason=error.code)
        self.wake.set();return self.get(identity)

    def pause_profile(self,profile):
        for row in self.rows():
            if row["config"]["profile"]["id"]==profile and row["state"]["status"]!="finished":self.pause(row["id"],reason="competition_credential_missing")

    def command(self,identity,value):
        try:
            with self.commands_lock:return self._command(identity,value)
        except WorkbenchError as error:
            get_logger("ctrl").warning("competition.control_rejected",session=identity,code=error.code,category="operator_control")
            raise

    def _command(self,identity,value):
        if not isinstance(value,dict) or "action" not in value:raise WorkbenchError("competition_control_invalid")
        action=value["action"];row=self.get(identity);state=row["state"]
        if state["status"]=="finished":raise WorkbenchError("competition_session_finished",409)
        if action=="pause" and set(value)=={"action"}:return self.pause(identity)
        if action=="finish" and set(value)=={"action"}:
            challenge=state.get("challenge_id")
            if challenge is not None:
                run=self.workbench.runner.status(challenge)
                if run is not None and run["status"] in {"starting","running","cancelling","cleaning"}:
                    raise WorkbenchError("competition_finish_requires_pause",409)
            abandoned=self._abandon(row,state)
            if challenge is not None:
                try:self.workbench.release(challenge)
                except WorkbenchError as error:
                    if error.code!="challenge_not_found":raise
            self.update(identity,status="finished",reason="operator_finished",pending_control=None,retry_operation=None,
                address_wait_started=None,address_wait_reason=None,question_abandoned=abandoned)
            self.wake.set();return self.get(identity)
        if action=="stop_after_current" and set(value)=={"action","enabled"} and type(value["enabled"]) is bool:
            self.update(identity,stop_after_current=value["enabled"]);self.wake.set();return self.get(identity)
        if action=="resume" and set(value)=={"action"}:
            if state["status"]=="pausing":raise WorkbenchError("competition_still_pausing",409)
            if self.clock()>=state["deadline_at"]:raise WorkbenchError("competition_overall_deadline",409)
            self.workbench.runner.reconcile_stopped_process(state["challenge_id"])
            self.workbench.competitions.credential_for(row["config"]["profile"])
            if state["current_ref"] is not None:
                item=self.read(row,state["current_ref"])
                if item is None:raise WorkbenchError("competition_question_not_found",409)
                if item["state"] not in {"active","available","completed","failed","expired","abandoned","pending","stopped","stop_pending"} and not item.get("is_completed"):
                    raise WorkbenchError("competition_question_not_active",409)
            challenge=state["challenge_id"]
            if challenge is not None:
                runtime=self.workbench.runner.runtime_control(challenge)
                if runtime and runtime["unknown"]:raise WorkbenchError("operator_unknown_operation",409)
                if runtime and runtime["status"] not in FINISHED and self.clock()<runtime["deadline_at"]:self.workbench.runner.operator_control(challenge,"resume")
            with self.workbench._db() as db:
                blocked=db.execute("SELECT operation_id FROM competition_platform_operations WHERE session_id=? AND receipt_json LIKE '%blocked%' ORDER BY rowid DESC LIMIT 1",(identity,)).fetchone()
            self.update(identity,status="running",reason=None,retry_operation=blocked[0] if blocked else None,submission_retry=True,
                address_wait_started=None,address_wait_reason=None)
            self.launch();return self.get(identity)
        if action in {"extend_question","extend_overall"} and set(value)=={"action","seconds","operation_id"}:
            seconds=value["seconds"];operation=value["operation_id"]
            try:valid=str(UUID(operation))==operation and UUID(operation).version==4
            except (TypeError,ValueError,AttributeError):valid=False
            if not valid or type(seconds) is not int or seconds<1:raise WorkbenchError("operator_extension_invalid")
            with self.workbench._db() as db:
                prior=db.execute("SELECT * FROM competition_platform_operations WHERE operation_id=?",(operation,)).fetchone()
                pending=db.execute("SELECT operation_id FROM competition_platform_operations WHERE session_id=? AND action IN ('extend_question','extend_overall') AND receipt_json LIKE '%pending%'",(identity,)).fetchone()
            if prior is not None:
                request=json.loads(prior["receipt_json"])
                if prior["session_id"]!=identity or prior["action"]!=action or request.get("seconds")!=seconds:raise WorkbenchError("operator_operation_conflict",409)
                if request["state"]=="applied":return self.get(identity)
            else:
                if pending is not None:raise WorkbenchError("operator_command_outcome_unknown",409)
                if self.clock()>=state["deadline_at"]:raise WorkbenchError("competition_overall_deadline",409)
                if action=="extend_question" and state["challenge_id"] is None:raise WorkbenchError("operator_run_missing",409)
                try:new_end=state["deadline_at"]+seconds
                except OverflowError:raise WorkbenchError("operator_extension_invalid") from None
                if not math.isfinite(new_end):raise WorkbenchError("operator_extension_invalid")
                cap=row["config"]["platform_deadline_at"]
                if action=="extend_overall" and cap is not None and new_end>cap:raise WorkbenchError("competition_extension_exceeds_platform",409)
                request=dict(seconds=seconds,deadline_at=new_end,challenge_id=state["challenge_id"],state="pending")
                if action=="extend_question" and self.workbench.runner.status(state["challenge_id"]) is None:
                    duration=(state.get("question_seconds") or row["config"]["question_seconds"])+seconds
                    caps=[state["deadline_at"]]
                    remaining=(state.get("platform") or {}).get("remaining_seconds")
                    if remaining is not None:caps.append(state["last_platform_check"]+remaining)
                    if self.clock()+duration>min(caps):raise WorkbenchError("operator_extension_exceeds_ceiling",409)
                    request["question_seconds"]=duration
                with self.workbench._db() as db:
                    db.execute("INSERT INTO competition_platform_operations VALUES (?,?,?,?,?,?,?)",(operation,identity,state["index"],action,"null",json.dumps(request),self.clock()))
            challenge=request["challenge_id"]
            runtime=self.workbench.runner.runtime_control(challenge) if challenge is not None else None
            current_ended=runtime is not None and (runtime["status"] in FINISHED or self.clock()>=runtime["deadline_at"])
            draft=challenge is not None and self.workbench.runner.status(challenge) is None
            if challenge is not None and not draft and not (action=="extend_overall" and current_ended):
                try:
                    result=self.workbench.runner.operator_control(challenge,action,seconds=seconds,operation_id=operation)
                    if result.get("pending"):
                        self.update(identity,pending_control=operation);return self.get(identity)
                except WorkbenchError as error:
                    if error.code in {"operator_command_outcome_unknown","operator_task_initializing"}:self.update(identity,pending_control=operation)
                    elif action=="extend_overall" and error.code=="operator_task_expired_or_terminal":
                        runtime=self.workbench.runner.runtime_control(challenge)
                        if runtime is None or runtime["status"] not in FINISHED and self.clock()<runtime["deadline_at"]:raise
                    else:
                        # A confirmed rejection is not an accepted extension.
                        with self.workbench._db() as db:db.execute("DELETE FROM competition_platform_operations WHERE operation_id=?",(operation,))
                        self.update(identity,pending_control=None)
                        raise
                    if error.code in {"operator_command_outcome_unknown","operator_task_initializing"}:raise
            with self.workbench.lock,self.workbench._db() as db:
                latest=self.get(identity)["state"]
                if action=="extend_overall":latest["deadline_at"]=request["deadline_at"]
                if "question_seconds" in request:latest["question_seconds"]=request["question_seconds"]
                latest["pending_control"]=None
                request["state"]="applied"
                db.execute("UPDATE competition_sessions SET state_json=? WHERE id=?",(json.dumps(latest),identity))
                db.execute("UPDATE competition_platform_operations SET receipt_json=? WHERE operation_id=?",(json.dumps(request),operation))
            self.wake.set();return self.get(identity)
        raise WorkbenchError("competition_control_invalid")

    def read(self,row,remote=None):
        profile=row["config"]["profile"]
        item=read_question(profile,self.workbench.competitions.credential_for(profile),remote)
        if remote is not None and item is None:raise WorkbenchError("competition_question_not_found",404)
        return item

    def perform(self,row,action,remote):
        row=self.live(row["id"]);identity=row["id"];slot=row["state"]["index"];ref=json.dumps(remote)
        with self.workbench.lock,self.workbench._db() as db:
            if self.get(identity)["state"]["status"]!="running":raise WorkbenchError("competition_session_not_running",409)
            prior=db.execute("SELECT * FROM competition_platform_operations WHERE session_id=? AND slot=? AND action=? AND remote_ref=? ORDER BY rowid DESC LIMIT 1",(identity,slot,action,ref)).fetchone()
        if prior is not None:
            receipt=json.loads(prior["receipt_json"]) if prior["receipt_json"] is not None else None
            if receipt is None or receipt.get("state")=="unknown":
                observed=self.read(row,remote) if action!="next" else self.read(row)
                if action=="next" and observed is not None and observed["state"]=="active":receipt={"item":observed,"observed_after_unknown":True}
                elif action=="start" and observed is not None and observed["state"]=="available" and observed["addresses"]:receipt={"addresses":observed["addresses"],"owned":False,"observed_after_unknown":True}
                elif action=="close" and observed is not None and observed["state"] in {"stopped","completed"} and not observed["addresses"]:receipt={"closed":True,"observed_after_unknown":True}
                else:raise WorkbenchError("competition_platform_operation_unknown",409)
                with self.workbench._db() as db:db.execute("UPDATE competition_platform_operations SET receipt_json=? WHERE operation_id=?",(json.dumps(receipt),prior["operation_id"]))
                return receipt
            if receipt.get("state")!="blocked":return receipt
            if row["state"].get("retry_operation")!=prior["operation_id"]:raise WorkbenchError(receipt["code"],409)
        operation=str(uuid4())
        with self.workbench.lock,self.workbench._db() as db:
            state=self.get(identity)["state"]
            if state["status"]!="running":raise WorkbenchError("competition_session_not_running",409)
            state["retry_operation"]=None
            db.execute("UPDATE competition_sessions SET state_json=? WHERE id=?",(json.dumps(state),identity))
            db.execute("INSERT INTO competition_platform_operations VALUES (?,?,?,?,?,?,?)",(operation,identity,slot,action,ref,None,self.clock()))
        profile=row["config"]["profile"]
        try:receipt=platform_action(profile,self.workbench.competitions.credential_for(profile),action,remote)
        except WorkbenchError as error:
            # A business refusal is a definite "no", not an unknown outcome; the
            # operator can retry it explicitly with 继续原赛程.
            known=error.code in {"competition_credential_missing","competition_auth_failed","competition_invalid_state",
                "competition_rate_limited","competition_request_rejected","competition_business_error"}
            receipt={"state":"blocked" if known else "unknown","code":error.code}
            if getattr(error,"platform_code",None) is not None:receipt["platform_code"]=error.platform_code
        with self.workbench._db() as db:db.execute("UPDATE competition_platform_operations SET receipt_json=? WHERE operation_id=?",(json.dumps(receipt),operation))
        get_logger("ctrl").info("competition.platform_operation",session=identity,operation=operation,action=action,category="platform_control",result=receipt.get("state","known"))
        if "state" in receipt:raise WorkbenchError(receipt["code"] if receipt["state"]=="blocked" else "competition_platform_operation_unknown",409)
        return receipt

    def target(self,row,item):
        from agent_core.turn_modeling import digest
        scope=digest({k:row["config"]["profile"][k] for k in ("id","adapter","base_url")})
        with self.workbench._db() as db:
            existing=db.execute("SELECT challenge_id FROM competition_questions WHERE scope_sha256=? AND remote_ref=?",(scope,json.dumps(item["remote_ref"]))).fetchone()
        if existing is not None:
            try:selected=self.workbench.get(existing[0])["endpoint"]
            except WorkbenchError as error:
                if error.code!="challenge_not_found":raise
                # The local question was deleted; drop the stale binding instead of
                # blocking the platform question for the rest of the session.
                with self.workbench._db() as db:db.execute("DELETE FROM competition_questions WHERE challenge_id=?",(existing[0],))
                selected=None
            if selected and any(_matches_target(raw,selected) for raw in item["addresses"]):return selected
        origins=[]
        for address in item["addresses"]:
            raw=address if "://" in address else row["config"]["default_scheme"]+"://"+address
            try:origin=_target_origin(raw)
            except (TypeError,ValueError):raise WorkbenchError("competition_target_protocol_unsupported",409) from None
            if origin not in origins:origins.append(origin)
        if len(origins)>1:raise WorkbenchError("competition_target_selection_required",409)
        return origins[0] if origins else ""

    def advance(self,row,*,resume=False):
        with self.workbench.lock:
            state=self.get(row["id"])["state"]
            changes=dict(index=state["index"]+1,current_ref=None,challenge_id=None,platform=None,last_platform_check=0,question_seconds=None,
                address_wait_started=None,address_wait_reason=None,address_wait_timeout_at=None)
            if state["stop_after_current"]:changes.update(status="finished",reason="competition_stopped_after_current",stop_after_current=False)
            elif resume and state["reason"]=="competition_platform_question_ended":changes.update(status="running",reason=None)
            return self.update(row["id"],**changes)

    def finish_question(self,row):
        self.update(row["id"],submission_retry=False)
        return self.workbench.runner.competition_end(row["state"]["challenge_id"])

    def _abandon(self,row,state):
        """Give the live attempt back when the platform protocol defines that action, so
        a later token or account is not blocked by a question this round never finished."""
        profile=row["config"]["profile"];remote=state.get("current_ref")
        if remote is None or not supports_action(profile["adapter"],"abandon"):return None
        known=(state.get("platform") or {}).get("state")
        if known is not None and known!="active":return None
        try:
            platform_action(profile,self.workbench.competitions.credential_for(profile),"abandon",remote)
            result="abandoned"
        except WorkbenchError as error:
            # A platform without the endpoint is not a failure of this round.
            result="unsupported" if getattr(error,"http_status",None) in {404,405} else error.code
        get_logger("ctrl").info("competition.attempt_abandon",session=row["id"],remote_ref=remote,
            result=result,category="platform_control")
        return result

    def tick(self):
        if not self.tick_lock.acquire(blocking=False):return
        try:
            for row in self.rows():
                if row["state"]["status"]=="finished":continue
                try:self.step(row)
                except WorkbenchError as error:
                    if error.code=="competition_session_not_running":continue
                    self.pause(row["id"],reason=error.code)
                break
        finally:self.tick_lock.release()

    def step(self,row):
        state=row["state"];identity=row["id"];runner=self.workbench.runner;challenge=state["challenge_id"]
        adapter=row["config"]["profile"]["adapter"]
        if state.get("pending_control"):
            with self.workbench._db() as db:
                pending=db.execute("SELECT action,receipt_json FROM competition_platform_operations WHERE operation_id=?",(state["pending_control"],)).fetchone()
            if pending is not None:
                request=json.loads(pending["receipt_json"])
                self.command(identity,dict(action=pending["action"],seconds=request["seconds"],operation_id=state["pending_control"]))
                row=self.get(identity);state=row["state"]
                if state.get("pending_control"):return
        active=False
        if challenge is not None:
            run=runner.status(challenge)
            active=run is not None and run["status"] in {"starting","running","cancelling","cleaning"}
        if not active and challenge is not None:
            reason=((run or {}).get("result") or {}).get("recovery",{}).get("reason")
            if reason=="competition_end_recovery_required" and not state.get("submission_retry"):
                self.update(identity,status="needs_attention",reason=reason);return
        if state["status"]!="running" and self.clock()>=state["deadline_at"] and not active:
            runtime=runner.runtime_control(challenge) if challenge else None
            if challenge is None or run is None or runtime and runtime["status"] in FINISHED:
                self.update(identity,status="finished",reason="competition_overall_deadline");return
            if runtime and not runtime["unknown"]:self.finish_question(row)
            else:self.update(identity,status="needs_attention",reason="competition_overall_deadline_unknown")
            return
        if state["status"]=="pausing":
            if not active:
                runtime=runner.runtime_control(challenge) if challenge else None
                self.update(identity,status="needs_attention" if runtime and runtime["unknown"] else "paused")
            return
        if state["status"] in {"paused","needs_attention"}:
            if state.get("reason")=="competition_platform_question_ended" and challenge is not None and not active:
                runtime=runner.runtime_control(challenge)
                if runtime and not runtime["unknown"]:
                    if runtime["status"] in FINISHED:
                        self.advance(row,resume=True)
                    else:self.finish_question(row)
                return
            if challenge is not None and not active:
                runtime=runner.runtime_control(challenge)
                if runtime and runtime["status"] not in FINISHED and self.clock()>=runtime["deadline_at"] and not runtime["unknown"]:
                    self.finish_question(row)
            return
        if self.clock()>=state["deadline_at"]:
            if challenge is None:self.update(identity,status="finished",reason="competition_overall_deadline")
            elif not active:
                runtime=runner.runtime_control(challenge)
                if runtime and runtime["status"] in FINISHED:self.update(identity,status="finished",reason="competition_overall_deadline")
                elif runtime and not runtime["unknown"]:self.finish_question(row)
                else:self.update(identity,status="needs_attention",reason="competition_overall_deadline_unknown")
            return
        if state["current_ref"] is None:
            if state["stop_after_current"]:self.update(identity,status="finished",reason="competition_stopped_after_current");return
            if adapter=="arena":
                item=self.read(row)
                if item is None or item["state"]!="active":
                    try:item=self.perform(row,"next",None)["item"]
                    except WorkbenchError as error:
                        if error.code!="competition_business_error":raise
                        # The platform may still hold an unended attempt; re-read once
                        # so a refusal does not strand the session on a stale answer.
                        item=self.read(row)
                        if item is None or item["state"]!="active":raise
            else:
                queue=row["config"]["queue"]
                if state["index"]>=len(queue):self.update(identity,status="finished",reason="competition_queue_completed");return
                item=self.read(row,queue[state["index"]])
                if item is None:raise WorkbenchError("competition_question_not_found",404)
                if item.get("is_completed"):self.advance(row);return
            with self.workbench.lock:
                latest=self.get(identity)["state"]
                if latest["status"]!="running":return
                if latest["stop_after_current"]:
                    self.update(identity,status="finished",reason="competition_stopped_after_current");return
                self.update(identity,current_ref=item["remote_ref"])
            row=self.get(identity);state=row["state"]
        waiting=state.get("address_wait_started") is not None
        due=self.clock()-state.get("last_platform_check",0)>=PLATFORM_POLL_SECONDS
        throttled=waiting and not due and challenge is None
        if challenge is None:
            item=None if throttled else self.read(row,state["current_ref"])
        else:
            item=self.read(row,state["current_ref"]) if due or not active else None
        if item is not None:
            self.update(identity,last_platform_check=self.clock(),platform={k:item[k] for k in ("state","correct_flag_count","flag_count","wrong_count","max_wrong_count","remaining_seconds") if k in item})
        if challenge is None or runner.status(challenge) is None:
            if item is None:
                if throttled:return
                raise WorkbenchError("competition_question_not_found",404)
            if adapter=="benchmark" and item["state"]=="stop_pending":return
            if adapter=="benchmark" and item["state"]!="available":
                receipt=self.perform(row,"start",item["remote_ref"])
                item=self.read(row,item["remote_ref"])
                if item is None or item["state"]!="available":return
            target=self.target(row,item);self.live(identity)
            # target() returns "" (not None) when the platform published no address.
            if not target and _should_wait_address(item,adapter):
                waited=state.get("address_wait_started")
                now=self.clock()
                if waited is None:
                    self.update(identity,address_wait_started=now,address_wait_reason="competition_waiting_for_address")
                    get_logger("ctrl").info("competition.address_wait_started",session=identity,remote_ref=item.get("remote_ref"),
                        question_category=item.get("category"),category="schedule_control")
                    return
                if now-waited<ADDRESS_WAIT_SECONDS:return
                self.update(identity,address_wait_started=None,address_wait_reason=None,address_wait_timeout_at=now)
                get_logger("ctrl").warning("competition.address_wait_timeout",session=identity,remote_ref=item.get("remote_ref"),
                    waited_seconds=round(now-waited,1),category="schedule_control")
            elif state.get("address_wait_started") is not None:
                self.update(identity,address_wait_started=None,address_wait_reason=None)
            task=self.workbench.competitions.import_question(row["config"]["profile"]["id"],dict(remote_ref=item["remote_ref"],endpoint=target,
                deadline_minutes=row["config"]["question_seconds"]//60,http_methods=row["config"]["http_methods"],
                default_description=row["config"].get("default_description",""),
                default_description_enabled=bool(row["config"].get("default_description_enabled"))),check_active=lambda:self.live(identity))
            self.update(identity,challenge_id=task["id"]);row=self.get(identity);state=row["state"];challenge=task["id"]
            if not target and not task["attachments"]:raise WorkbenchError("competition_question_input_required",409)
            self.live(identity)
            run=runner.status(challenge)
            # A terminal run (finished/failed/cancelled/...) is history, not a live
            # claim; only an unresolved run blocks a fresh import of the same question.
            if run is not None and run["status"] in ACTIVE_RUN_STATUSES:raise WorkbenchError("competition_existing_task_requires_recovery",409)
            runner.start(challenge,mode=row["config"]["mode"],competition_control={"session_id":identity,"question_seconds":state.get("question_seconds") or row["config"]["question_seconds"],"overall_deadline_at":state["deadline_at"],"overall_ceiling_at":row["config"]["platform_deadline_at"]})
            return
        if item is not None and item["state"] in {"failed","expired","abandoned"}:
            self.pause(identity,reason="competition_platform_question_ended");return
        if active:
            if item is not None and (item.get("is_completed") or item["state"]=="solved"):
                if runner.inflight_operation(challenge)=="competition-submit":return
                self.pause(identity,reason="competition_completion_requires_verification");return
            if item is not None and item["state"] not in {"active","available"} and not item.get("is_completed"):
                self.pause(identity,reason="competition_target_unavailable")
            return
        run=runner.status(challenge);result=(run or {}).get("result") or {}
        task_status=(result.get("task_run") or {}).get("status")
        if task_status in FINISHED:
            if state["stop_after_current"] and task_status!="achieved":
                self.advance(self.get(identity));return
            if task_status=="deadline_exceeded" and row["config"]["profile"]["adapter"]=="arena" and item is not None and item["state"]=="active":
                self.update(identity,status="needs_attention",reason="competition_arena_local_deadline_active");return
            if task_status=="achieved" and row["config"]["profile"]["adapter"]=="benchmark":
                if item is None or not item.get("is_completed"):raise WorkbenchError("competition_completion_conflict",409)
                with self.workbench._db() as db:
                    starts=db.execute("SELECT receipt_json FROM competition_platform_operations WHERE session_id=? AND action='start' AND remote_ref=?",(identity,json.dumps(state["current_ref"]))).fetchall()
                if any(r[0] is not None and json.loads(r[0]).get("owned") is True for r in starts):self.perform(row,"close",state["current_ref"])
            self.advance(self.get(identity));return
        if result.get("recovery",{}).get("required"):raise WorkbenchError("competition_task_recovery_required",409)
        control=runner.runtime_control(challenge)
        if control and self.clock()>=control["deadline_at"] and not control["unknown"]:
            self.finish_question(row);return
        if control and control["control"] and control["control"]["paused"]:return
        if task_status=="awaiting_external_feedback":
            submission=result.get("competition_submission",{}).get("state")
            if submission=="unknown" or submission=="blocked" and not state.get("submission_retry"):raise WorkbenchError("competition_submission_requires_attention",409)
            self.update(identity,submission_retry=False)
            runner.competition_submit(challenge)
        elif task_status=="running":runner.resume(challenge)
        else:raise WorkbenchError("competition_task_start_failed",409)

    def shutdown(self):
        for row in self.rows():
            if row["state"]["status"] in {"running","pausing"}:self.pause(row["id"],reason="competition_workbench_stopped")
        self.closed.set();self.wake.set()
        if self.thread is not None and self.thread is not threading.current_thread():self.thread.join(timeout=1)
