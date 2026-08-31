#!/usr/bin/env python3
"""
云效代码审计 - 多维效能指标计算（双轨评分）
读取 audit_data.json + commit_timestamps.json (+ 可选 defects_data.json + 可选 effort_data.json)，
产出 enhanced_audit_data.json。

评分维度（固定 7 维）：
  总有效产出 30% + 缺陷密度 30% + 日均产出 15% + 工时投入 10% + 删行占比 5% + 提交颗粒度 5% + 连续性 5%
  （主维度用总量而非"日均÷活跃天"；缺陷密度=缺陷数/千有效行，负向指标越低越好；
   工时投入=周期内登记的实际任务工时，"投入"辅助维度，正向；
   日均产出作辅助维度补充活跃日效率视角）
维度降级规则：
  - 无 defects_data.json → 自动剔除缺陷密度维度并把权重分摊到其余维度
  - 无 effort_data.json（文件完全缺失）→ 自动剔除工时投入维度并分摊权重
  - 注意：只要存在工时数据（effort_data.json）即固定纳入工时维度（7维固定），
    不再用填报率门槛剔除——填报率偏低仅作透明提示，不降级。
双轨评分：
  Track 1 组内排名（铁数据）：同职能组内百分位，原始数据，不折算
  Track 2 全员折算排名（参考值）：前端行数类指标 × 系数后，全员百分位

===== 使用前必须修改下面的 CONFIG 区 =====
根据实际团队填写 ROLE_MAP / IDENTITY_MERGES / EXCLUDE_DEVS。
缺陷数据可选：若 workdir 下有 defects_data.json（由 fetch_defects.py 产出，按中文姓名聚合），
则自动启用缺陷密度维度；否则该维度权重会被自动摊到其他维度。
工时数据可选：若 workdir 下有 effort_data.json（由 fetch_effort.py 产出，按中文姓名聚合），
则固定纳入工时投入维度（7维固定），不再用填报率门槛剔除——填报率偏低仅作透明提示。

用法:
  python3 compute_metrics.py --workdir <workdir>
"""
import argparse
import json
import os
from datetime import datetime
from collections import defaultdict

# ============================================================
# ===== CONFIG：使用前根据团队实际情况修改 =====
# ============================================================

# 职能映射：邮箱 -> "frontend" / "backend"。未列出的默认按 backend 处理。
ROLE_MAP = {
    # "someone@example.com": "frontend",
    # "other@example.com": "backend",
}
ROLE_LABELS = {"frontend": "前端", "backend": "后端"}

# 身份合并：把系统自动账号/多域名邮箱映射到真人显示名
IDENTITY_MERGES = {
    # "accounts_<hash>@mail.teambition.com": "张三",
    # "zhangsan@sina.com": "张三",
}

# 跨邮箱同人合并（关键）：同一个人用了多个"独立邮箱"提交，需在统计层面合并。
# 与 IDENTITY_MERGES 的区别：IDENTITY_MERGES 只映射显示名；DEV_KEY_MERGES 会把
# secondary 邮箱的 commits/行数/活跃天/仓库/needs 全部累加到 primary 邮箱，并删除 secondary。
# 常见触发场景：个人邮箱 + 公司邮箱、主邮箱 + teambition 自动账号。跨时间窗拉长时尤其容易暴露。
# 格式： { 次要邮箱: 主邮箱 }
DEV_KEY_MERGES = {
    # "someone@company.com": "someone@personal.com",
}

# 显示名覆盖：把 git 里的英文/账号名改为正式姓名。格式 { 主邮箱: "正式姓名" }
# 缺陷/工时按中文姓名匹配，必须用云效一致的中文姓名
DISPLAY_NAME_OVERRIDES = {
    # "someone@example.com": "张三",
}

# 要排除的开发者（邮箱），如数据量过少的人
EXCLUDE_DEVS = set([
    # "someone@qq.com",
])

# 前端折算系数：跨职能对比时前端行数类指标乘以此系数（前端代码约膨胀1.5倍 → 取0.6）
FRONTEND_COEFFICIENT = 0.6
LINE_BASED_DIMS = {"total_output", "daily_output", "commit_gran"}  # 受系数影响的行数类维度

# 负向维度：值越小越好（如缺陷密度）。归一化时会反转百分位（低值 = 高分）。
NEGATIVE_DIMS = {"defect_density", "deletion_ratio"}
# 注：deletion_ratio 历史上作为正向（重构/清理能力）处理，此处不放入反转集合，
#     仅 defect_density 作为负向指标。deletion_ratio 保持原正向语义。
NEGATIVE_DIMS = {"defect_density"}

# 评分维度权重（改这里可增减维度；键须与下方 raw_metrics 计算一致）
# 注意：主维度用 total_output（周期内有效增删行总数），而非 daily_output（÷活跃天）——
#      "活跃天"只统计有提交的天数，会让勤奋者(活跃天多)日均被摊薄、突击者虚高，故用总量口径。
#      daily_output 作为低权重辅助维度保留（补充"活跃日效率"视角）。
#      defect_density（缺陷密度 = 缺陷数/千有效行）为负向质量维度：单位产出的缺陷越少评分越高，
#      用比值而非绝对数量，避免"写得多的人缺陷绝对数自然多"的不公平。
#      work_hours（工时投入 = 周期内登记的实际任务工时）为"投入"辅助维度：正向，
#      权重 10%（投入非产出，刻意低于产出类维度）；只要存在工时数据即固定纳入（7维）。
DIMENSION_WEIGHTS = {
    "total_output": 30,
    "defect_density": 30,
    "daily_output": 15,
    "work_hours": 10,
    "deletion_ratio": 5,
    "commit_gran": 5,
    "streak": 5,
}
# 若无缺陷数据(defects_data.json)，是否自动剔除 defect_density 维度并把其权重按比例分摊到其余维度
AUTO_DROP_DEFECT_IF_MISSING = True
# 若无工时数据(effort_data.json)，是否自动剔除 work_hours 维度并分摊权重
AUTO_DROP_EFFORT_IF_MISSING = True
# 工时填报率门槛：填报率(有工时的工作项占比) 低于此值(百分比)时，自动剔除工时维度。
# 理由：工时填报率过低时，大量人是 0 工时，强行归一化只会制造噪声、误伤未填报者。
EFFORT_MIN_FILL_RATE = 40.0
# ============================================================


def normalize_to_score(value, all_values, negative=False):
    """百分位归一化到 0-100。negative=True 时反转（值越小分越高，用于缺陷密度等负向指标）。"""
    if not all_values:
        return 50
    sv = sorted(all_values)
    n = len(sv)
    if negative:
        # 值越小排名越高：统计有多少个 >= value
        rank = sum(1 for v in sv if v >= value)
    else:
        rank = sum(1 for v in sv if v <= value)
    return max(0, min(100, round(rank / n * 100, 1)))


def compute_streak(active_days):
    if not active_days:
        return 0
    ds = sorted(active_days)
    ms = cs = 1
    for i in range(1, len(ds)):
        pd = datetime.strptime(ds[i-1], "%Y-%m-%d")
        cd = datetime.strptime(ds[i], "%Y-%m-%d")
        if (cd - pd).days <= 1:
            cs += 1; ms = max(ms, cs)
        else:
            cs = 1
    return ms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', required=True)
    args = ap.parse_args()
    wd = args.workdir

    with open(os.path.join(wd, "audit_data.json"), 'r') as f:
        base_data = json.load(f)
    with open(os.path.join(wd, "commit_timestamps.json"), 'r') as f:
        ts_data = json.load(f)

    # 可选：缺陷数据（按中文姓名聚合），用于缺陷密度维度
    defects_by_name = {}
    defects_meta = None
    dpath = os.path.join(wd, "defects_data.json")
    if os.path.exists(dpath):
        with open(dpath, 'r') as f:
            ddata = json.load(f)
        defects_meta = ddata.get("metadata")
        for name, d in ddata.get("defects_by_person", {}).items():
            defects_by_name[name] = d.get("count", 0)
        print(f"✅ 已加载缺陷数据: {sum(defects_by_name.values())} 个 (窗口 {defects_meta.get('days') if defects_meta else '?'} 天)")

    # 可选：工时数据（按中文姓名聚合），用于工时投入维度。受填报率门槛把关。
    hours_by_name = {}
    effort_meta = None
    effort_enabled = False
    epath = os.path.join(wd, "effort_data.json")
    if os.path.exists(epath):
        with open(epath, 'r') as f:
            edata = json.load(f)
        effort_meta = edata.get("metadata")
        for name, d in edata.get("effort_by_person", {}).items():
            hours_by_name[name] = d.get("hours", 0)
        fill_rate = (effort_meta or {}).get("fill_rate", 0)
        total_h = (effort_meta or {}).get("total_hours", 0)
        # 工时维度固定纳入（7维）：只要存在工时数据即启用，不再用填报率门槛剔除
        effort_enabled = True
        if fill_rate < EFFORT_MIN_FILL_RATE:
            print(f"⚠️ 工时填报率 {fill_rate}% < {EFFORT_MIN_FILL_RATE}%（数据偏稀疏），仍按 7 维固定纳入工时投入维度")
        else:
            print(f"✅ 已加载工时数据: {total_h}h (填报率 {fill_rate}%，启用工时维度)")

    # 动态维度权重：无缺陷/工时数据时按配置剔除对应维度并把权重按比例分摊到其余维度
    dim_weights = dict(DIMENSION_WEIGHTS)

    def _drop_and_redistribute(dim, reason):
        if dim not in dim_weights:
            return
        dropped = dim_weights.pop(dim)
        remain = sum(dim_weights.values())
        for k in dim_weights:
            dim_weights[k] = round(dim_weights[k] + dropped * dim_weights[k] / remain, 2)
        print(f"⚠️ {reason}，已剔除 {dim} 维度，权重({dropped})分摊到其余维度")

    if "defect_density" in dim_weights and not defects_by_name and AUTO_DROP_DEFECT_IF_MISSING:
        _drop_and_redistribute("defect_density", "未找到缺陷数据")
    if "work_hours" in dim_weights and not effort_enabled and AUTO_DROP_EFFORT_IF_MISSING:
        _drop_and_redistribute("work_hours", "工时数据缺失或填报率过低")

    commits = ts_data.get("commits", [])
    dev_stats = base_data["developer_stats"]

    # 排除指定开发者
    for ekey in list(dev_stats.keys()):
        if ekey in EXCLUDE_DEVS:
            del dev_stats[ekey]
            base_data["identity_map"].pop(ekey, None)
            print(f"✅ 已排除 {ekey}")
    commits = [c for c in commits if c["author_email"] not in EXCLUDE_DEVS]
    base_data["metadata"]["unique_developers"] = len(dev_stats)

    # 跨邮箱同人合并：把 secondary 的统计累加到 primary 后删除 secondary
    _SUM_FIELDS = ["commits", "merge_commits", "effective_additions",
                   "effective_deletions", "net_effective"]
    _UNION_FIELDS = ["repos", "active_days", "needs", "raw_ids"]
    for sec, pri in DEV_KEY_MERGES.items():
        if sec in dev_stats and pri in dev_stats:
            s, p = dev_stats[sec], dev_stats[pri]
            for f in _SUM_FIELDS:
                p[f] = p.get(f, 0) + s.get(f, 0)
            for f in _UNION_FIELDS:
                p[f] = sorted(set(p.get(f, [])) | set(s.get(f, [])))
            del dev_stats[sec]
            if sec in base_data["identity_map"]:
                base_data["identity_map"][pri] = list(base_data["identity_map"].get(pri, [])) + base_data["identity_map"].pop(sec)
            print(f"✅ 已合并 {sec} -> {pri}")
    # 时间线里的次要邮箱重定向到主邮箱
    for c in commits:
        if c["author_email"] in DEV_KEY_MERGES:
            c["author_email"] = DEV_KEY_MERGES[c["author_email"]]

    # 显示名覆盖
    for _k, _name in DISPLAY_NAME_OVERRIDES.items():
        if _k in dev_stats:
            dev_stats[_k]["display_name"] = _name

    base_data["metadata"]["unique_developers"] = len(dev_stats)

    # email -> dev key
    email_to_key = {}
    for key, s in dev_stats.items():
        for rid in s.get("raw_ids", []):
            em = rid.split("<")[1].split(">")[0].strip() if "<" in rid and ">" in rid else rid.strip()
            email_to_key[em] = key

    def find_key(email):
        if email in email_to_key:
            return email_to_key[email]
        if email in IDENTITY_MERGES:
            merged = IDENTITY_MERGES[email]
            for k, s in dev_stats.items():
                if s.get("display_name") == merged:
                    return k
        return None

    # 构建每人 commit 时间线
    dev_commits = defaultdict(list)
    for c in commits:
        email = c["author_email"]
        key = find_key(email)
        ts = c.get("committed_date") or c.get("created_at") or ""
        if not ts:
            continue
        try:
            from datetime import timedelta
            tsc = ts.replace("Z", "+00:00")
            if "+" in tsc[10:]:
                dt = datetime.fromisoformat(tsc)
                dt_local = dt.astimezone(tz=None)
            else:
                dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
                dt_local = dt + timedelta(hours=8)
        except Exception:
            continue
        dev_commits[key or email].append({
            "hour": dt_local.hour, "weekday_num": dt_local.weekday(),
            "week": dt_local.strftime("%Y-W%W"),
        })

    # raw metrics
    raw = {}
    defect_count_by_key = {}
    hours_by_key = {}
    for key, s in dev_stats.items():
        nd = len(s["active_days"]); tl = s["effective_additions"] + s["effective_deletions"]
        nc = s["commits"]; dels = s["effective_deletions"]
        # 缺陷密度：缺陷数 / 千有效行(KLOC)。缺陷按 display_name(中文姓名) 关联。
        dn = s.get("display_name", "")
        dcount = defects_by_name.get(dn, 0)
        defect_count_by_key[key] = dcount
        defect_density = dcount / max(tl / 1000.0, 0.001)  # 每千行缺陷数
        # 工时投入：周期内登记的实际任务工时(小时)。按 display_name(中文姓名) 关联。
        whours = hours_by_name.get(dn, 0)
        hours_by_key[key] = whours
        raw[key] = {
            "total_output": tl,
            "daily_output": tl / max(nd, 1),
            "commit_gran": tl / max(nc, 1),
            "deletion_ratio": dels / max(tl, 1) * 100,
            "streak": compute_streak(s["active_days"]),
        }
        if defects_by_name:
            raw[key]["defect_density"] = defect_density
        if effort_enabled:
            raw[key]["work_hours"] = whours

    # 折算后 metrics
    adjusted = {}
    for key, r in raw.items():
        role = ROLE_MAP.get(key, "backend")
        adjusted[key] = {d: (v * FRONTEND_COEFFICIENT if role == "frontend" and d in LINE_BASED_DIMS else v)
                         for d, v in r.items()}

    role_groups = defaultdict(list)
    for key in dev_stats:
        role_groups[ROLE_MAP.get(key, "backend")].append(key)

    role_pools = defaultdict(dict)
    for role, keys in role_groups.items():
        for d in dim_weights:
            role_pools[role][d] = [raw[k][d] for k in keys]
    cross_pools = {d: [adjusted[k][d] for k in dev_stats] for d in dim_weights}

    enhanced = {}
    for key, s in dev_stats.items():
        role = ROLE_MAP.get(key, "backend")
        nd = len(s["active_days"]); tl = s["effective_additions"] + s["effective_deletions"]
        nc = s["commits"]; dels = s["effective_deletions"]; adds = s["effective_additions"]
        intra = {d: normalize_to_score(raw[key][d], role_pools[role][d], negative=(d in NEGATIVE_DIMS)) for d in dim_weights}
        cross = {d: normalize_to_score(adjusted[key][d], cross_pools[d], negative=(d in NEGATIVE_DIMS)) for d in dim_weights}
        intra_c = round(sum(intra[d] * dim_weights[d] / 100 for d in dim_weights), 1)
        cross_c = round(sum(cross[d] * dim_weights[d] / 100 for d in dim_weights), 1)

        tl_list = dev_commits.get(key, [])
        wk = defaultdict(int)
        for cc in tl_list:
            wk[cc["week"]] += 1
        tot = sum(wk.values()) or 1

        def _week_to_date(wlabel):
            try:
                y, w = wlabel.split("-W")
                return datetime.strptime(f"{y}-{int(w)}-1", "%Y-%W-%w").strftime("%Y-%m-%d")
            except Exception:
                return wlabel

        weekly_trend = [{"week": w, "week_start": _week_to_date(w), "lines": round(tl * wk[w] / tot, 1), "commits": wk[w]}
                        for w in sorted(wk.keys())]
        tcat = {"上午(9-12)": 0, "下午(13-18)": 0, "晚间(19-23)": 0, "凌晨/早间(0-8)": 0}
        for cc in tl_list:
            h = cc["hour"]
            if 9 <= h <= 12: tcat["上午(9-12)"] += 1
            elif 13 <= h <= 18: tcat["下午(13-18)"] += 1
            elif 19 <= h <= 23: tcat["晚间(19-23)"] += 1
            else: tcat["凌晨/早间(0-8)"] += 1
        wdl = ["周一","周二","周三","周四","周五","周六","周日"]
        wdd = defaultdict(int)
        for cc in tl_list:
            wdd[cc["weekday_num"]] += 1
        weekday_dist = {wdl[i]: wdd.get(i, 0) for i in range(7)}

        enhanced[key] = {
            "display_name": s["display_name"], "email": s["email"],
            "role": role, "role_label": ROLE_LABELS.get(role, role),
            "commits": nc, "effective_additions": adds, "effective_deletions": dels,
            "net_effective": s["net_effective"], "total_effective": tl,
            "active_days": nd, "repos_count": len(s["repos"]),
            "raw_ids": s["raw_ids"], "repos": s["repos"], "active_days_list": s["active_days"],
            "daily_output": round(tl / max(nd, 1), 1),
            "daily_net": round(s["net_effective"] / max(nd, 1), 1),
            "commit_granularity": round(tl / max(nc, 1), 1),
            "deletion_ratio": round(dels / max(tl, 1) * 100, 1),
            "add_del_ratio": round(adds / max(dels, 1), 2),
            "longest_streak": raw[key]["streak"],
            "adjusted_total_output": round(adjusted[key]["total_output"], 1),
            "adjusted_daily_output": round(adjusted[key]["total_output"] / max(nd, 1), 1),
            "adjusted_commit_granularity": round(adjusted[key]["commit_gran"], 1),
            "defect_count": defect_count_by_key.get(key, 0),
            "defect_density": round(raw[key].get("defect_density", 0), 2) if defects_by_name else None,
            "work_hours": round(hours_by_key.get(key, 0), 1) if effort_enabled else None,
            "intra_scores": intra, "intra_composite": intra_c,
            "cross_scores": cross, "cross_composite": cross_c,
            "scores": intra, "composite_score": intra_c,
            "weekly_trend": weekly_trend, "time_distribution": tcat,
            "weekday_distribution": weekday_dist, "dimension_weights": dim_weights,
        }

    for role, keys in role_groups.items():
        for rank, k in enumerate(sorted(keys, key=lambda k: enhanced[k]["intra_composite"], reverse=True), 1):
            enhanced[k]["role_rank"] = rank
            enhanced[k]["role_total"] = len(keys)
    all_keys = sorted(dev_stats.keys(), key=lambda k: enhanced[k]["cross_composite"], reverse=True)
    for rank, k in enumerate(all_keys, 1):
        enhanced[k]["cross_rank"] = rank
        enhanced[k]["cross_total"] = len(all_keys)

    out = {
        "metadata": base_data["metadata"], "repos": base_data["repos"],
        "active_repos": base_data["active_repos"], "developer_stats": enhanced,
        "identity_map": base_data["identity_map"], "dimension_weights": dim_weights,
        "role_map": {k: {"role": ROLE_MAP.get(k, "backend"),
                         "label": ROLE_LABELS.get(ROLE_MAP.get(k, "backend"), "")} for k in dev_stats},
        "role_groups": {role: [enhanced[k]["display_name"] for k in keys]
                        for role, keys in role_groups.items()},
        "frontend_coefficient": FRONTEND_COEFFICIENT, "line_based_dims": list(LINE_BASED_DIMS),
        "negative_dims": list(NEGATIVE_DIMS),
        "defects_meta": defects_meta,
        "effort_meta": effort_meta,
        "effort_enabled": effort_enabled,
    }
    path = os.path.join(wd, "enhanced_audit_data.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"✅ 已保存: {path}")

    print(f"\n=== 双轨评分（前端系数={FRONTEND_COEFFICIENT}）维度权重={dim_weights} ===")
    has_defect = "defect_density" in dim_weights
    has_effort = "work_hours" in dim_weights
    print("\n【Track 1 组内排名（铁数据）】")
    for role, label in ROLE_LABELS.items():
        if not role_groups.get(role):
            continue
        print(f"  {label}:")
        for k in sorted(role_groups[role], key=lambda k: enhanced[k]["intra_composite"], reverse=True):
            s = enhanced[k]
            dd = f" 缺陷={s['defect_count']}(密度{s['defect_density']}/KLOC)" if has_defect else ""
            wh = f" 工时={s['work_hours']}h" if has_effort else ""
            print(f"    #{s['role_rank']} {s['display_name']:12s} 评分={s['intra_composite']} "
                  f"总产出={s['total_effective']} 日均={s['daily_output']} 删行%={s['deletion_ratio']} 粒度={s['commit_granularity']} 连续={s['longest_streak']}{dd}{wh}")
    print("\n【Track 2 全员折算排名（参考值）】")
    for rank, k in enumerate(all_keys, 1):
        s = enhanced[k]
        print(f"  #{rank} [{s['role_label']}] {s['display_name']:12s} 评分={s['cross_composite']}")
    if effort_meta and not has_effort:
        print(f"\n⚠️ 工时维度未纳入评分：填报率 {effort_meta.get('fill_rate')}% 低于门槛 {EFFORT_MIN_FILL_RATE}%。")


if __name__ == "__main__":
    main()
