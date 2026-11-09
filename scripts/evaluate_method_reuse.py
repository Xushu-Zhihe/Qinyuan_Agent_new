"""Opt-in, bounded real-model microbenchmark of same-task method retrieval."""
import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_adapters.deepseek_model import DeepSeekModelAdapter
from agent_adapters.deepseek_roles import _raw_http_worker, settings_from_file, role_config
from agent_core.modeling import ModelError

LIMIT = 12


def evaluate(output):
    from agent_core.stage_planning import obj, text
    settings = settings_from_file(role_config('inner_action'), Path('.env'))
    records = json.loads(output.read_text('utf-8')) if output.exists() else []
    adapter = DeepSeekModelAdapter(settings, worker=_raw_http_worker)
    def save():
        output.write_text(json.dumps(records, ensure_ascii=False, indent=2), 'utf-8')
    def call(key, messages, choices):
        previous = next((r for r in records if r['key'] == key), None)
        if previous is not None:
            if previous['status'] == 'started':
                raise RuntimeError('unsettled evaluation call: do not replay')
            return previous.get('proposal')
        if len(records) >= LIMIT:
            raise RuntimeError('authorized call limit reached')
        row = {'key': key, 'model': settings.model, 'effort': 'high', 'status': 'started'}
        records.append(row); save()
        print('Starting evaluation call', len(records), key, flush=True)
        payload = {'model': settings.model, 'thinking': {'type': 'enabled'}, 'reasoning_effort': 'high',
            'messages': messages, 'stream': True, 'tool_choice': 'auto', 'tools': [{'type': 'function',
                'function': {'name': 'submit_stage_result', 'description': 'Propose exactly one bounded result.',
                             'parameters': obj({'result': {'anyOf': choices}})}}]}
        diagnostic = {}
        start = time.monotonic()
        raw = adapter._invoke_payload(payload, settings.timeout_seconds, SimpleNamespace(is_cancelled=False), diagnostic=diagnostic)
        row.update(seconds=round(time.monotonic()-start, 3), usage=diagnostic.get('usage'),
                   transport_end=diagnostic.get('transport_end'))
        if isinstance(raw, ModelError):
            row.update(status='transport_error', code=raw.short_reason); save()
            raise RuntimeError('evaluation transport failed: ' + raw.short_reason)
        try:
            calls = raw.get('tool_calls') or []
            value = json.loads(calls[0]['function']['arguments']) if calls else json.loads(raw['content'])
            proposal = value.get('result', value)
            from agent_core.output_contracts import shape_issues
            if shape_issues(proposal, {'anyOf': choices}):
                raise ValueError('invalid proposal')
            row.update(status='completed', proposal=proposal, finish_reason=raw.get('finish_reason'),
                       generated_script_characters=len(proposal.get('arguments', {}).get('script', '')))
        except (KeyError, IndexError, ValueError, TypeError):
            row.update(status='invalid_output', finish_reason=raw.get('finish_reason'))
        save()
        print('Finished', key, row['status'], row['seconds'], flush=True)
        return row.get('proposal')
    cases = [
        ('decode', "import base64,json,zlib\nfrom pathlib import Path\n"
         "data=zlib.decompress(base64.b64decode(Path('/workspace/work/input.b64').read_bytes()))\n"
         "value=json.loads(data)\nprint(json.dumps({'count':len(value['items']),'sum':sum(value['items'])}))\n",
         'Decode base64+zlib JSON input.b64; report item count and sum. Recorded result: count=4, sum=10.'),
        ('csv', "import csv,json\nfrom collections import defaultdict\nfrom pathlib import Path\n"
         "totals=defaultdict(int)\nwith Path('/workspace/work/input.csv').open(newline='') as f:\n"
         "    for row in csv.DictReader(f):\n        totals[row['group']]+=int(row['value'])\n"
         "print(json.dumps(dict(sorted(totals.items()))))\n",
         'Aggregate input.csv by group with integer sums. Recorded result: a=6, b=4.')]
    for number, (name, script, goal) in enumerate(cases):
        for arm in (('baseline', 'methods') if number == 0 else ('methods', 'baseline')):
            ref = 'method:synthetic:' + name
            choices = [obj({'kind': {'type':'string','const':'history_details'}, 'refs': {'type':'array','items':{'type':'string','const':'observation:'+name},'minItems':1,'maxItems':1}}),
                       obj({'kind': {'type':'string','const':'mcp_call'}, 'tool_name':{'type':'string','const':'shell.run_bash'}, 'arguments':obj({'script':text()})})]
            context = {'task':'synthetic-method-reuse', 'goal':goal,
                       'state':'Input bytes, runtime, target scope and workspace are unchanged; the original script worked.',
                       'history_catalog':[{'ref':'observation:'+name,'summary':'successful original local execution; original arguments retrievable by history_details'}]}
            if arm == 'methods':
                choices.append(obj({'kind':{'type':'string','const':'reuse_method'},'method_ref':{'type':'string','const':ref}}))
                context['methods']=[{'ref':ref, 'purpose':goal, 'conditions':'Same unchanged local input and runtime; original execution and method reviewed accepted.'}]
            messages = [{'role':'system','content':'Propose the next action to obtain the same local result. Prefer a supported existing method or retrieve original code before rewriting. Do not access network, invent observations, or broaden the task. submit_stage_result only proposes an action; it executes nothing.'},
                        {'role':'user','content':json.dumps(context)}]
            proposal = call(name+'/'+arm+'/decision', messages, choices)
            if proposal and proposal['kind']=='history_details':
                messages.append({'role':'user','content':json.dumps({'previous_proposal':proposal, 'history_details':{'tool_name':'shell.run_bash','arguments':{'script':"python3 - <<'PY'\n"+script+'PY\n'}}})})
                proposal = call(name+'/'+arm+'/after_read', messages, [c for c in choices if c['properties']['kind']['const']!='history_details'])
            if proposal:
                review_choices=[obj({'decision':{'type':'string','enum':['accept','revise']},'reason':text()})]
                call(name+'/'+arm+'/review', [{'role':'system','content':'Review whether this proposal can obtain the declared result from the unchanged local input. A supplied method is a trusted same-task reviewed reference. Reject scope expansion, network access or fabrication. This is a synthetic static review, not execution proof.'},
                    {'role':'user','content':json.dumps({'goal':goal, 'known_working_script':script, 'allowed_method_ref':ref if arm=='methods' else None,'proposal':proposal})}], review_choices)
    print(json.dumps({'calls':len(records), 'output':str(output)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args()
    evaluate(args.output.resolve())
