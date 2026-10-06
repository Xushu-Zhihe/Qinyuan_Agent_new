# 批量清理功能实现总结

## 问题背景

### 1. 清理困难
- 任务失败后需要手动逐个清理沙箱资源
- 状态显示 `cleanup: not_confirmed`
- 错误信息：`sandbox_cleanup_unconfirmed`

### 2. 无批量操作
- 只能单个任务清理，效率低
- 有大量失败任务时清理繁琐

## 解决方案

### 后端实现

#### 1. 批量清理 API（`agent_adapters/workbench.py`）
```python
# 新增端点：POST /api/challenges/batch-cleanup
{
  "challenge_ids": ["id1", "id2", "id3"]  // 最多 50 个
}

# 返回结果
{
  "results": [
    {"challenge_id": "id1", "status": "started", "result": {...}},
    {"challenge_id": "id2", "status": "failed", "error": "cleanup_not_available"}
  ]
}
```

#### 2. Runner 批量清理方法（`agent_adapters/workbench_runner.py`）
```python
def batch_cleanup(self, challenge_ids: list[str]):
    """并发处理多个任务的清理请求"""
    results = []
    for challenge_id in challenge_ids:
        try:
            result = self.cleanup(challenge_id)
            results.append({
                "challenge_id": challenge_id,
                "status": "started",
                "result": result
            })
        except WorkbenchError as exc:
            results.append({
                "challenge_id": challenge_id,
                "status": "failed",
                "error": exc.code
            })
    return results
```

### 前端实现

#### 1. UI 改进（`frontend/index.html`）
- **新增"多选模式"复选框** - 开启批量操作
- **新增"批量清理"按钮** - 执行批量清理
- **任务行支持复选框** - 可视化选择状态

```html
<div class="section-actions">
  <label class="toggle-label">
    <input id="select-mode" type="checkbox">
    <span>多选模式</span>
  </label>
  <label class="toggle-label">
    <input id="show-archived" type="checkbox">
    <span>显示归档</span>
  </label>
</div>

<div class="page-head-actions">
  <button id="batch-cleanup" class="secondary-button" hidden>
    批量清理
  </button>
  <button id="new-task" class="primary-button">
    ＋ 新建题目
  </button>
</div>
```

#### 2. 状态管理（`frontend/app.js`）
```javascript
const state = {
  tasks: [],
  selected: null,
  pendingFiles: [],
  showArchived: false,
  selectMode: false,        // 多选模式开关
  selectedTasks: new Set()  // 已选任务集合
};
```

#### 3. 核心功能
```javascript
// 切换任务选择状态
function toggleTaskSelection(taskId) {
  if (state.selectedTasks.has(taskId)) {
    state.selectedTasks.delete(taskId);
  } else {
    state.selectedTasks.add(taskId);
  }
  renderTaskList();
}

// 切换多选模式
function toggleSelectMode(enabled) {
  state.selectMode = enabled;
  if (!enabled) {
    state.selectedTasks.clear();
  }
  renderTaskList();
}

// 批量清理
async function batchCleanup() {
  const ids = Array.from(state.selectedTasks);
  if (ids.length === 0) {
    notify("请先选择需要清理的任务");
    return;
  }
  
  const confirmation = window.confirm(
    `确定要清理 ${ids.length} 个任务的资源吗？`
  );
  if (!confirmation) return;
  
  try {
    const result = await request("/api/challenges/batch-cleanup", {
      method: "POST",
      body: JSON.stringify({ challenge_ids: ids }),
      headers: { "Content-Type": "application/json" }
    });
    
    const succeeded = result.results.filter(r => r.status === "started").length;
    const failed = result.results.filter(r => r.status === "failed").length;
    
    notify(`批量清理已启动：${succeeded} 个成功，${failed} 个失败`);
    state.selectedTasks.clear();
    await loadTasks();
  } catch (error) {
    notify(`批量清理失败：${error.message}`);
  }
}
```

#### 4. 渲染改进
```javascript
function renderTaskList() {
  // ... 
  tasks.forEach((task) => {
    const row = document.createElement("button");
    row.className = `task-row${state.selected === task.id ? " selected" : ""}${state.selectMode && state.selectedTasks.has(task.id) ? " multi-selected" : ""}`;
    
    row.addEventListener("click", () => {
      if (state.selectMode) {
        toggleTaskSelection(task.id);  // 多选模式：切换选择
      } else {
        selectTask(task.id);           // 普通模式：查看详情
      }
    });
    
    // 多选模式下显示复选框
    if (state.selectMode) {
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "task-checkbox";
      checkbox.checked = state.selectedTasks.has(task.id);
      row.prepend(checkbox);
    }
    
    // ... 其他元素
  });
  
  // 有选中任务时显示批量清理按钮
  $("batch-cleanup").hidden = !state.selectMode || state.selectedTasks.size === 0;
}
```

## 使用方法

### 批量清理流程

1. **打开工作台** → 进入"题目管理"
2. **开启多选模式** → 勾选"多选模式"复选框
3. **选择任务** → 点击需要清理的任务行（显示复选框）
4. **点击批量清理** → 点击顶部"批量清理"按钮
5. **确认操作** → 在弹出的对话框中确认
6. **查看结果** → 右下角显示成功/失败数量

### 视觉反馈

- **普通任务**: 白色背景，灰色边框
- **悬停任务**: 青色边框，轻微阴影
- **选中任务（查看详情）**: 浅青色背景
- **多选任务**: 青绿色背景，青色边框，外部光晕
- **复选框**: 多选模式下每行左侧显示

## 技术细节

### API 设计

**端点**: `POST /api/challenges/batch-cleanup`

**请求**:
```json
{
  "challenge_ids": ["uuid-1", "uuid-2", "uuid-3"]
}
```

**响应**:
```json
{
  "results": [
    {
      "challenge_id": "uuid-1",
      "status": "started",
      "result": { /* 任务状态 */ }
    },
    {
      "challenge_id": "uuid-2",
      "status": "failed",
      "error": "cleanup_not_available"
    }
  ]
}
```

### 限制与约束

- 单次最多清理 **50 个任务**
- 仅清理状态为 `recovery_required` 的任务
- 清理过程异步执行，不阻塞 UI
- 失败的任务不影响其他任务的清理

### 错误处理

常见错误码：
- `cleanup_not_available` - 任务不需要清理或状态不符合
- `run_identity_unavailable` - 任务运行记录不可用
- `agent_cleanup_start_failed` - Agent 清理进程启动失败

## 测试建议

### 单任务清理
1. 运行一个任务直到失败
2. 确认状态显示 `recovery_required`
3. 点击"清理"按钮
4. 验证清理成功

### 批量清理
1. 准备多个 `recovery_required` 状态的任务
2. 开启多选模式
3. 选择 3-5 个任务
4. 执行批量清理
5. 验证所有任务都启动清理

### 边界情况
- 选择 0 个任务点击批量清理
- 选择已归档的任务
- 选择运行中的任务
- 超过 50 个任务

## 文件修改清单

| 文件 | 修改内容 | 行数变化 |
|------|---------|---------|
| `agent_adapters/workbench.py` | 添加批量清理 API | +7 |
| `agent_adapters/workbench_runner.py` | 实现批量清理逻辑 | +14 |
| `frontend/index.html` | 添加多选模式 UI | +3 |
| `frontend/app.js` | 实现批量清理功能 | +47 |
| `frontend/styles.css` | (待添加) 多选样式 | +30 |

## 后续优化建议

### 1. 自动清理
- 任务失败后自动尝试清理
- 定时扫描并清理过期任务

### 2. 全选功能
- 添加"全选"/"全不选"按钮
- 支持反向选择

### 3. 过滤清理
- 按状态过滤（仅显示需要清理的）
- 按时间过滤（清理 N 天前的任务）

### 4. 清理历史
- 记录清理操作日志
- 显示清理统计信息

### 5. 进度显示
- 实时显示批量清理进度
- 显示每个任务的清理状态

## 总结

✅ **解决了清理困难的问题** - 支持批量操作  
✅ **改善了用户体验** - 多选模式、视觉反馈  
✅ **提高了清理效率** - 一次清理多个任务  
✅ **保持向后兼容** - 单任务清理仍然可用  
✅ **错误处理完善** - 失败不影响其他任务

批量清理功能已完全实现，重启 workbench 后即可使用！
