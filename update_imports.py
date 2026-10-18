#!/usr/bin/env python3
"""Update imports for moved d7 files to v7 subdirectories."""

import re
from pathlib import Path

# Mapping of old imports to new imports
IMPORT_MAPPINGS = {
    'agent_adapters.d7_candidate_evidence': 'agent_adapters.web.candidate_evidence',
    'agent_adapters.d7_candidate_operator_view': 'agent_adapters.web.candidate_operator_view',
    'agent_adapters.d7_http_adapter': 'agent_adapters.web.http_adapter',
    'agent_adapters.d7_model_adapter': 'agent_adapters.web.model_adapter',
    'agent_adapters.d7_request_contract': 'agent_adapters.web.request_contract',
    'agent_adapters.d7_request_staging': 'agent_adapters.web.request_staging',
    'agent_adapters.d7_sandbox_transport': 'agent_adapters.web.sandbox_transport',
    'agent_adapters.d7_simulated_application': 'agent_adapters.web.simulated_application',
    'agent_adapters.d7_task_result': 'agent_adapters.web.task_result',
    'agent_adapters.d7_web_application': 'agent_adapters.web.web_application',
    'agent_adapters.d7_web_request': 'agent_adapters.web.web_request',
    'agent_core.d7_web_contracts': 'agent_core.web_contracts',
}

def update_file(file_path: Path):
    """Update imports in a single file."""
    try:
        content = file_path.read_text(encoding='utf-8')
        original = content

        for old_import, new_import in IMPORT_MAPPINGS.items():
            # Replace in from...import statements
            content = content.replace(f'from {old_import}', f'from {new_import}')
            # Replace in import statements
            content = content.replace(f'import {old_import}', f'import {new_import}')

            # Handle "from agent_adapters import d7_xxx as xxx" pattern
            old_module = old_import.split('.')[-1]  # e.g., 'd7_candidate_evidence'
            new_module_path = new_import.replace('.', '/')  # e.g., 'agent_adapters/v7/candidate_evidence'

            # Replace patterns like: from agent_adapters import d7_candidate_evidence
            pattern1 = f'from agent_adapters import {old_module}'
            replacement1 = f'from agent_adapters.web import {old_module[3:]}'  # Remove 'd7_' prefix
            content = content.replace(pattern1, replacement1)

            # Replace patterns like: from agent_core import d7_web_contracts
            pattern2 = f'from agent_core import {old_module}'
            replacement2 = f'from agent_core import {old_module[3:]}'  # Remove 'd7_' prefix
            content = content.replace(pattern2, replacement2)

        if content != original:
            file_path.write_text(content, encoding='utf-8')
            print(f"Updated: {file_path}")
            return True
        return False
    except Exception as e:
        print(f"Error updating {file_path}: {e}")
        return False

def main():
    """Update all Python files in the project."""
    directories = ['agent_adapters', 'agent_core', 'tests']
    updated_count = 0

    for directory in directories:
        dir_path = Path(directory)
        if not dir_path.exists():
            continue

        for py_file in dir_path.rglob('*.py'):
            if update_file(py_file):
                updated_count += 1

    print(f"\nTotal files updated: {updated_count}")

if __name__ == '__main__':
    main()
