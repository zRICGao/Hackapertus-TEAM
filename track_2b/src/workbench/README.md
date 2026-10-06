# Apertus Workbench 基础框架

独立于完整上游工作台，复用固定版本的 Memory/Idea 存储、Observer 类型和状态表格。资产清单见根目录 management/REUSE_ASSETS.md。

```powershell
# 在项目根执行
python scripts/extract_breachweave.py --check
./.tools/node_modules/.bin/bun.cmd test ./workbench/apertus-workbench/tests
./.tools/node_modules/.bin/bun.cmd run ./workbench/apertus-workbench/src/demo.ts ./experiments/runtime-state/demo
```

最后命令输出 schema_version=1.0 的快照，可由 Python runtime_adapter.normalize_snapshot 读取。适配器保留上游状态但只产生 candidate，不将 Idea verified 当作 finding。

API/MCP 和 Planner 模型尚未配置，ports 中保留明确接口并抛出未配置错误。当前没有 Web UI、后台服务、Docker Solver 或真实模型请求；完整上游应用仍单独保留。
