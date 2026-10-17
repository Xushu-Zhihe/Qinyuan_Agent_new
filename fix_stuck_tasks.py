"""Simple script to fix stuck HTTP tasks with incomplete operations."""

import sys
import json
import sqlite3
import time
from pathlib import Path

def fix_stuck_http_task(run_path: Path):
    """Fix a stuck HTTP task by completing incomplete D4 operations and executions."""

    agent_dirs = list(run_path.glob('agent-state/*'))
    agent_dirs = [d for d in agent_dirs if d.is_dir() and d.name != 'requests']

    if not agent_dirs:
        print(f"No agent-state found in {run_path}")
        return False

    agent_path = agent_dirs[0]
    db_path = agent_path / 'control.sqlite'

    if not db_path.exists():
        print(f"No control.sqlite found")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Find incomplete D4 operations
    cursor.execute('''
        SELECT operation_id, category, start_json
        FROM d4_operations
        WHERE completion_json IS NULL
    ''')
    incomplete_ops = cursor.fetchall()

    if not incomplete_ops:
        print("No incomplete operations found")
        conn.close()
        return False

    print(f"Found {len(incomplete_ops)} incomplete operations")

    for op_id, category, start_json in incomplete_ops:
        print(f"  Fixing operation {op_id[:16]}... ({category})")

        # Complete the D4 operation with 'unknown' status
        completion = {
            'operation_id': op_id,
            'completed_at': time.time(),
            'result_kind': 'unknown',
            'evidence_id': op_id,
            'evidence_digest': ''
        }

        cursor.execute('''
            UPDATE d4_operations
            SET completion_json = ?
            WHERE operation_id = ?
        ''', (json.dumps(completion), op_id))

        # If there's a stuck execution, mark it as failed
        cursor.execute('''
            UPDATE executions
            SET status = 'failed'
            WHERE execution_id = ? AND status = 'running'
        ''', (op_id,))

        if cursor.rowcount > 0:
            print(f"    Updated execution status to 'failed'")

    conn.commit()
    conn.close()

    print(f"Fixed {len(incomplete_ops)} operations")
    return True

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python fix_stuck_tasks.py <run_id> [run_id2 ...]")
        print("Example: python fix_stuck_tasks.py 1fa415e7-4c10-4025-9559-363931d56bec")
        sys.exit(1)

    workbench_root = Path('workbench_data/runs')

    for run_id in sys.argv[1:]:
        run_path = workbench_root / run_id

        if not run_path.exists():
            print(f"\nRun {run_id}: NOT FOUND")
            continue

        print(f"\n{'='*60}")
        print(f"Fixing run: {run_id}")
        print('='*60)

        try:
            success = fix_stuck_http_task(run_path)
            if success:
                print(f"\n✓ Successfully fixed run {run_id}")
            else:
                print(f"\n- Nothing to fix in run {run_id}")
        except Exception as e:
            print(f"\n✗ Error fixing run {run_id}: {e}")
            import traceback
            traceback.print_exc()
