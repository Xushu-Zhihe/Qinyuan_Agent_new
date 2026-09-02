import json
from collections import Counter
from typing import Any
from nuclei_mcp.config import SEVERITY_ORDER

def load_jsonl_text(text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]]=[]

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue

        if isinstance(payload,dict):
            findings.append(payload)
    
    return findings

def summarize_findings (
    findings: list[dict[str, Any]],
    sample_limit: int = 50,
) -> dict[str, Any]:
    sample_limit = min(sample_limit, 200)
    severity_counts: Counter[str] = Counter()
    sample_findings: list[dict[str, Any]] = []
    actionable_findings: list[dict[str, Any]] = []
    highest_severity = "unknown"

    for finding in findings:
        info = finding.get("info",{})
        if not isinstance(info, dict):
            info = {}

        severity = str(info.get("severity", "unknown")).lower()
        severity_counts[severity] += 1

        compact_finding = {
            "template_id": finding.get("template-id"),
            "name": info.get("name"),
            "severity": severity,
            "type": finding.get("type"),
            "host": finding.get("host"),
            "matched_at": finding.get("matched-at"),
            "matcher_name": finding.get("matcher-name"),
        }

        if len(sample_findings) < sample_limit:
            sample_findings.append(compact_finding)

        if severity in {"medium", "high", "critical"}:
            actionable_findings.append(compact_finding)

        if SEVERITY_ORDER.get(severity, 0) > SEVERITY_ORDER.get(highest_severity, 0):
            highest_severity = severity
        
    return {
        "finding_count": len(findings),
        "severity_counts": dict(severity_counts),
        "sample_findings": sample_findings,
        "actionable_findings": actionable_findings,
        "has_actionable": len(actionable_findings) > 0,
        "highest_severity": highest_severity,
    }        