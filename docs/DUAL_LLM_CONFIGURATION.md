# Dual-LLM Role Configuration

## Overview

The workbench frontend now supports **dual-LLM role configuration**, allowing you to use two separate models working in a coordinated architecture:

- **Outer Role (Planning)**: High-level decision making, task planning, and result review
- **Inner Role (Execution)**: Tool invocation, code execution, and observation recording

## Architecture

### Role Separation

The dual-LLM architecture implements a clear separation of concerns:

1. **Outer Planning Role** (`outer_role`)
   - Analyzes task requirements and constraints
   - Creates execution plans and intent strategies  
   - Reviews inner role actions before execution
   - Makes high-level decisions (continue, replan, adjust)
   - Validates results and evidence

2. **Inner Action Role** (`inner_role`)
   - Executes specific operations and tool calls
   - Interacts with the sandbox environment
   - Records observations and evidence
   - Performs concrete actions like file operations, network requests
   - Reports results back to outer role for validation

### Data Contracts

Both roles use the `D1RoleConfig` contract defined in `agent_core/turn_contracts.py`:

```python
@dataclass(frozen=True, slots=True)
class D1RoleConfig:
    role: D1Role  # "outer_planning" or "inner_action"
    provider: str  # e.g., "deepseek", "openai_compatible"
    model: str  # Model identifier
    prompt_template: str  # Role-specific prompt
    parameters: tuple[tuple[str, str | int | float | bool], ...]
```

The dual configuration is wrapped in `D3RunConfig` (see `agent_core/dual_llm_contracts.py`):

```python
@dataclass(frozen=True, slots=True)
class D3RunConfig:
    outer_role: D1RoleConfig
    inner_role: D1RoleConfig
    authorization: D3Authorization
    review_role_ref: str
    # ... additional configuration
```

## Frontend Implementation

### UI Components

The frontend (`frontend/index.html`) now includes:

1. **Mode Toggle**: Switch between single-model and dual-LLM modes
   - Single Model Mode: Traditional one-model configuration
   - Dual LLM Mode: Separate outer/inner role configuration

2. **Dual Role Cards**: Visual distinction between roles
   - Outer Role Card: Planning model with ◐ icon
   - Inner Role Card: Execution model with ◑ icon
   - Visual separator (⇄) showing role interaction

3. **Configuration Fields**:
   - Outer Model Name: Planning model identifier
   - Inner Model Name: Execution model identifier
   - Shared API Key: Single key for both models

### Visual Design

The UI uses a clean, intuitive design:

- **Color Scheme**: 
  - Outer role: Lighter teal gradient (#f8fcfb)
  - Inner role: Softer green gradient (#fafcfb)
  
- **Icons**:
  - ◐ (Left half circle): Represents outer/planning layer
  - ◑ (Right half circle): Represents inner/execution layer
  - ⇄ (Bidirectional arrow): Shows role coordination

- **Responsive Layout**: Adapts to mobile/tablet screens

## Backend Implementation

### API Endpoint

The `/api/model` endpoint now supports both configuration modes:

**Single Model (Legacy)**:
```json
PUT /api/model
{
  "provider": "deepseek",
  "model": "deepseek-chat",
  "base_url": "https://api.deepseek.com",
  "api_key": "sk-..."
}
```

**Dual-LLM**:
```json
PUT /api/model
{
  "outer_model": "deepseek-chat",
  "inner_model": "deepseek-chat",
  "api_key": "sk-..."
}
```

### Model Status Response

The model status endpoint returns:

```json
GET /api/model
{
  "provider": "deepseek",
  "model": "deepseek-chat",
  "base_url": "https://api.deepseek.com",
  "key_configured": true,
  "protocol_version": 3,
  "dual_llm_enabled": true,
  "outer_model": "deepseek-chat",
  "inner_model": "deepseek-chat"
}
```

### Backend Code

Updated files:
- `agent_adapters/workbench.py`: Added dual-LLM configuration support
  - `model_status()`: Returns dual-LLM state
  - `configure_model()`: Handles both single and dual modes

## Usage Guide

### Configuring Dual-LLM

1. Navigate to "模型配置" (Model Configuration) in the workbench
2. Click "双 LLM 角色模式" (Dual LLM Role Mode) button
3. Enter outer role model name (e.g., `deepseek-chat`)
4. Enter inner role model name (e.g., `deepseek-chat`)
5. Enter shared API key
6. Click "应用双角色配置" (Apply Dual Role Configuration)

### Use Cases

**Same Model for Both Roles**:
- Useful for development and testing
- Both outer and inner use `deepseek-chat`
- Simpler configuration, consistent behavior

**Different Models**:
- Outer: Larger, more capable model for planning (e.g., GPT-4)
- Inner: Faster, cheaper model for execution (e.g., GPT-3.5)
- Optimizes cost vs. capability trade-off

## Technical Details

### Protocol Version

Dual-LLM configuration is associated with **Protocol Version 3 (D3)**, which:
- Supports multi-turn task execution
- Implements outer/inner role separation
- Includes budget tracking (D4)
- Enables execution authorization (D6)

### Control Flow

1. **Task Initialization**: `D3RunConfig` created with both roles
2. **Turn Start**: Outer role reviews task and creates plan
3. **Intent Execution**: Inner role executes specific operations
4. **Action Review**: Outer role validates inner actions
5. **Result Verification**: Outer role confirms observations
6. **Turn Completion**: Outer role decides next steps

### Evidence and Validation

The dual-LLM architecture improves evidence quality:
- Inner role generates raw observations
- Outer role validates observation correctness
- Cross-validation reduces hallucination
- Explicit review steps create audit trail

## Files Modified

### Frontend
- `frontend/index.html`: Added dual-LLM UI components
- `frontend/styles.css`: Added dual-LLM styling
- `frontend/app.js`: Added dual-LLM configuration handlers

### Backend
- `agent_adapters/workbench.py`: Extended model configuration API

### Core Contracts (Reference Only)
- `agent_core/turn_contracts.py`: D1RoleConfig definition
- `agent_core/dual_llm_contracts.py`: D3RunConfig definition

## Future Enhancements

Potential improvements:
1. Per-role prompt template customization
2. Per-role parameter tuning (temperature, max_tokens)
3. Role-specific base URL configuration
4. Visual execution flow diagram
5. Role performance metrics and analytics
6. A/B testing different role combinations

## References

- D1 Protocol: Single-turn role configuration
- D3 Protocol: Dual-LLM multi-turn execution
- D4 Protocol: Budget tracking and limits
- D6 Protocol: Execution authorization and artifacts

## Migration Guide

### From Single Model

Existing single-model configurations remain fully supported. To migrate:

1. Note your current model name
2. Switch to dual-LLM mode
3. Use the same model for both roles initially
4. Experiment with different combinations as needed

### Backward Compatibility

The implementation maintains backward compatibility:
- Legacy single-model API continues working
- Old configurations auto-load in single-model mode
- Protocol version determines configuration mode
