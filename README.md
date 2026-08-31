# yunxiao-code-audit

基于阿里云云效（Yunxiao / Codeup）API 的研发效能审计工具：统计开发者的"有效代码行数"（过滤锁文件、编译产物、二进制、纯格式化改动等噪声），并结合缺陷密度、任务工时等维度做 7 维加权评分，最终产出可视化 HTML 报告。

这是一个 WorkBuddy / Claude Skill（见 `SKILL.md`），也可独立作为 Python 脚本集使用。

## 核心特性

- **有效行数统计**：不统计 `package-lock.json`、`dist/`、`.min.js`、图片字体等噪声，只算真实手写代码
- **7 维评分**（固定权重，合计 100%）：
  | 维度 | 权重 | 方向 |
  |---|---|---|
  | 总有效产出 | 30% | 正向 |
  | 缺陷密度（缺陷数/千有效行） | 30% | 负向 |
  | 日均产出 | 15% | 正向 |
  | 工时投入 | 10% | 正向 |
  | 删行占比（重构清理） | 5% | 正向 |
  | 提交颗粒度 | 5% | 正向 |
  | 连续性 | 5% | 正向 |
- **双轨评分**：Track 1 组内排名（同职能百分位，铁数据）+ Track 2 全员折算排名（前端行数 × 系数后跨职能对比，参考值）
- **身份归一化**：自动合并同人多邮箱、teambition 系统账号
- **数据驱动降级**：无缺陷/工时数据时自动剔除对应维度并把权重分摊到其余维度

## 目录结构

```
├── SKILL.md                        # Skill 入口：完整工作流与 API 速查
├── scripts/
│   ├── collect.py                  # Step1: 拉取提交 + Compare API 解析有效行数
│   ├── fetch_timestamps.py         # Step2: 拉取提交时间戳（连续性/日均产出）
│   ├── fetch_defects.py            # Step3: 拉取缺陷（缺陷密度维度）
│   ├── fetch_effort.py             # Step4: 拉取任务工时（任务负责人归口，实际优先预计兜底）
│   ├── compute_metrics.py          # Step5: 7 维评分计算（需按团队填 CONFIG 区）
│   ├── collect_filtered.py         # collect 的过滤增强版
│   ├── fetch_timestamps_filtered.py# 时间戳过滤增强版
│   └── generate_report.py          # Step6: 渲染 HTML 报告
├── assets/
│   └── report_template.html        # 报告模板（数据驱动，维度列动态显隐）
└── references/
    └── effective_lines_rules.md    # 有效行数过滤规则详解
```

## 快速开始

```bash
# 1. 采集提交与有效行数（--days 30 = 最近一个月）
python3 scripts/collect.py --token <云效PAT> --org-id <orgId> --days 30 --out <workdir>

# 2. 提交时间戳
python3 scripts/fetch_timestamps.py --token <PAT> --org-id <orgId> --workdir <workdir>

# 3. 缺陷（可选，有则启用缺陷密度维度）
python3 scripts/fetch_defects.py --token <PAT> --org-id <orgId> --space-id <projectId> --days 30 --workdir <workdir>

# 4. 任务工时（可选，有则启用工时维度；按任务负责人归口）
python3 scripts/fetch_effort.py --token <PAT> --org-id <orgId> --space-id <projectId> --days 30 --by assignedTo --workdir <workdir>

# 5. 计算指标（先按你的团队填 compute_metrics.py 的 CONFIG 区）
python3 scripts/compute_metrics.py --workdir <workdir>

# 6. 生成 HTML 报告
python3 scripts/generate_report.py --workdir <workdir> \
  --template assets/report_template.html \
  --data <workdir>/enhanced_audit_data.json \
  --out <workdir>/audit_report.html
```

> Token 从云效「个人设置 → 个人访问令牌」创建，需要 Codeup 与项目协作（Projex）读取权限。
> **切勿把 token、组织 ID、团队成员真实邮箱提交进仓库**——`compute_metrics.py` 的 CONFIG 区按团队填写后请自行保管。

## 依赖

- Python 3.9+（`requests` 为唯一第三方依赖）

## 许可

内部工具，未指定开源许可；如需复用请联系作者。
