"""Fix all stuck HTTP tasks in workbench_data/runs."""

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
        return False, "No agent-state found"

    agent_path = agent_dirs[0]
    db_path = agent_path / 'control.sqlite'

    if not db_path.exists():
        return False, "No control.sqlite found"

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Find incomplete D4 operations
    cursor.execute('''
        SELECT operation_id, category
        FROM d4_operations
        WHERE completion_json IS NULL
    ''')
    incomplete_ops = cursor.fetchall()

    if not incomplete_ops:
        conn.close()
        return False, "No incomplete operations"

    for op_id, category in incomplete_ops:
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

    conn.commit()
    conn.close()

    return True, f"Fixed {len(incomplete_ops)} operations"

if __name__ == '__main__':
    workbench_root = Path('workbench_data/runs')

    if not workbench_root.exists():
        print("workbench_data/runs directory not found")
        sys.exit(1)

    # Find all runs
    all_runs = [d for d in workbench_root.iterdir() if d.is_dir()]

    print(f"Scanning {len(all_runs)} runs...")
    fixed_count = 0

    for run_path in all_runs:
        success, message = fix_stuck_http_task(run_path)
        if success:
            print(f"[OK] {run_path.name}: {message}")
            fixed_count += 1

    print(f"\nFixed {fixed_count} stuck tasks")
