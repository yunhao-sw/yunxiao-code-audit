#!/usr/bin/env python3
"""
云效代码审计 - 任务工时(Effort)数据采集
通过云效项目协作(projex)工作项 API 拉取每个工作项的工时明细，按「任务负责人(assignedTo)」聚合，
用于计算「人均任务工时」投入维度。

数据口径与重要提醒：
  - 工时 API 是"按单个工作项"查询：
    · 实际工时 GET .../workitems/{id}/effortRecords，返回 actualTime(小时) + creator/owner。
    · 预计工时 GET .../workitems/{id}/estimatedEfforts，字段名可能是 estimatedTime/spentTime/actualTime。
  - 因此需先列出周期内所有工作项(需求/任务/缺陷)，再逐个查工时明细。
  - 取值规则：每个工作项的有效工时 = 实际工时之和(>0 优先)，若为 0 则用预计工时之和兜底
    (可用 --no-estimate-fallback 关闭兜底)。
  - 归属：有效工时归到该工作项的「任务负责人 assignedTo」(工作项级归口)。
  - 工时属"投入"而非"产出/质量"指标，权重天然不宜高(建议5%)；且很多团队工时填报率极低。
  - 本脚本会统计"填报率"(有有效工时的工作项占比)与"预计兜底占比"。若填报率过低，
    compute_metrics.py 会根据阈值(EFFORT_MIN_FILL_RATE)自动剔除工时维度并分摊权重，
    避免用大片空白数据制造噪声。

归属口径(--by)：
  - assignedTo(默认，推荐)：按任务负责人聚合，工作项级；支持实际+预计兜底。与缺陷维度归属统一。
  - creator/owner：按实际工时记录的登记人/记录owner聚合，记录级；仅对实际工时有效，不做预计兜底。

用法:
  python3 fetch_effort.py --token <PAT> --org-id <orgId> --days 30 --workdir <workdir>
  # 可选：--space-id <projectId> 指定项目；不填则纳入组织下所有项目
  # 可选：--by assignedTo|creator|owner  工时归属口径，默认 assignedTo
  # 可选：--categories Req,Task,Bug  纳入统计的工作项类型，默认全部三类
  # 可选：--no-estimate-fallback  关闭"实际为0用预计兜底"

注意：工时统计窗口应与代码采集窗口(collect.py --days)保持一致。
"""
import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests

MAX_RETRIES = 4


def make_headers(token):
    return {"x-yunxiao-token": token, "Content-Type": "application/json"}


def req(method, url, headers, **kw):
    """带指数退避重试的请求。"""
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            return requests.request(method, url, headers=headers, timeout=30, **kw)
        except requests.exceptions.RequestException as e:
            last = e
            wait = 1.2 * (2 ** attempt)
            print(f"  ⚠️ 网络异常({type(e).__name__})，{wait:.0f}s 后重试({attempt+1}/{MAX_RETRIES})")
            time.sleep(wait)
    print(f"  ❌ 重试失败: {url} — {last}")
    return None


def list_projects(base, org_id, headers):
    url = f"{base}/oapi/v1/projex/organizations/{org_id}/projects:search"
    projects, page = [], 1
    while True:
        r = req("POST", url, headers, json={"page": page, "perPage": 100})
        if not r or r.status_code != 200:
            print(f"  ⚠️ 列项目 HTTP {r.status_code if r else 'None'}: {(r.text[:200] if r else '')}")
            break
        data = r.json()
        items = data if isinstance(data, list) else (data.get("result") or [])
        if not items:
            break
        projects.extend(items)
        if len(items) < 100:
            break
        page += 1
        time.sleep(0.2)
    return projects


def search_items(base, org_id, headers, space_id, category):
    """分页拉取某项目下某类型的所有工作项。"""
    url = f"{base}/oapi/v1/projex/organizations/{org_id}/workitems:search"
    items, page = [], 1
    while True:
        body = {"category": category, "spaceId": space_id, "spaceType": "Project",
                "orderBy": "gmtCreate", "page": page, "perPage": 100, "sort": "desc"}
        r = req("POST", url, headers, json=body)
        if not r or r.status_code != 200:
            print(f"  ⚠️ 搜工作项 cat={category} HTTP {r.status_code if r else 'None'}: {(r.text[:150] if r else '')}")
            break
        data = r.json()
        batch = data if isinstance(data, list) else (data.get("workitems") or data.get("result") or [])
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        time.sleep(0.15)
    return items


def get_effort_records(base, org_id, headers, workitem_id):
    url = f"{base}/oapi/v1/projex/organizations/{org_id}/workitems/{workitem_id}/effortRecords"
    r = req("GET", url, headers)
    if not r or r.status_code != 200:
        return []
    data = r.json()
    if isinstance(data, list):
        return data
    return data.get("result") or data.get("effortRecords") or []


def get_estimated_efforts(base, org_id, headers, workitem_id):
    """预计工时明细。端点 estimatedEfforts；字段名可能是 estimatedTime/spentTime/actualTime。"""
    url = f"{base}/oapi/v1/projex/organizations/{org_id}/workitems/{workitem_id}/estimatedEfforts"
    r = req("GET", url, headers)
    if not r or r.status_code != 200:
        return []
    data = r.json()
    if isinstance(data, list):
        return data
    return data.get("result") or data.get("estimatedEfforts") or []


def _sum_effort(records):
    """从工时记录列表求和(小时)。兼容 actualTime/estimatedTime/spentTime 字段名。"""
    total = 0.0
    for x in records:
        v = x.get("actualTime")
        if v is None:
            v = x.get("estimatedTime")
        if v is None:
            v = x.get("spentTime")
        total += v or 0
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True, help="云效 PAT（需 项目协作 只读权限）")
    ap.add_argument("--org-id", required=True)
    ap.add_argument("--base", default="https://openapi-rdc.aliyuncs.com")
    ap.add_argument("--days", type=int, default=30, help="回溯天数，默认30（须与 collect.py 一致）")
    ap.add_argument("--space-id", default=None, help="项目ID(spaceId)；不填则纳入组织下所有项目")
    ap.add_argument("--by", default="assignedTo", choices=["assignedTo", "creator", "owner"],
                    help="工时归属口径，默认按任务负责人 assignedTo（工作项级）。"
                         "creator/owner 为工时记录级归属(登记人/记录owner)，仅对实际工时有效。")
    ap.add_argument("--categories", default="Req,Task,Bug",
                    help="纳入的工作项类型(逗号分隔)，默认 Req,Task,Bug")
    ap.add_argument("--no-estimate-fallback", action="store_true",
                    help="关闭'实际工时为0时用预计工时兜底'（默认开启兜底）")
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()
    os.makedirs(args.workdir, exist_ok=True)

    headers = make_headers(args.token)
    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    now = datetime.now(timezone.utc) + timedelta(hours=8)  # 北京时间
    cutoff = now - timedelta(days=args.days)
    cutoff_ms = int((cutoff - timedelta(hours=8)).replace(tzinfo=timezone.utc).timestamp() * 1000)

    print("=" * 60)
    print(f"云效工时采集 | 窗口: 最近{args.days}天 (>= {cutoff.strftime('%Y-%m-%d')}) | 归属: {args.by} | 类型: {categories}")
    print("=" * 60)

    # 项目列表
    if args.space_id:
        space_ids = [args.space_id]
        proj_names = {args.space_id: args.space_id}
    else:
        projs = list_projects(args.base, args.org_id, headers)
        space_ids = [p["id"] for p in projs]
        proj_names = {p["id"]: p.get("name", p["id"]) for p in projs}
        print(f"发现 {len(space_ids)} 个项目: {[proj_names[s] for s in space_ids]}")

    # 汇总窗口内所有工作项
    all_items = []
    for sid in space_ids:
        for cat in categories:
            its = search_items(args.base, args.org_id, headers, sid, cat)
            win = [it for it in its if (it.get("gmtCreate") or 0) >= cutoff_ms]
            for it in win:
                it["_cat"] = cat
            all_items.extend(win)
            print(f"  项目 {proj_names.get(sid, sid)} / {cat}: 窗口内 {len(win)} 项")

    use_est_fallback = not args.no_estimate_fallback
    by_owner_item = (args.by == "assignedTo")  # True=工作项级(负责人)归口；False=记录级(登记人/owner)
    print(f"\n窗口内工作项总数: {len(all_items)}，开始逐个拉取工时明细...")
    print(f"取值口径: 实际工时优先" + ("，无实际则用预计工时兜底" if use_est_fallback else "（已关闭预计兜底）"))

    # 聚合容器
    hours_by_person = defaultdict(float)          # 有效工时(小时)
    records_by_person = defaultdict(int)          # 关联工作项数(工作项级) / 记录条数(记录级)
    by_cat = defaultdict(lambda: {"items": 0, "with_effort": 0, "hours": 0.0})
    items_with_effort = 0     # 有"有效工时"(>0)的工作项数
    items_by_estimate = 0     # 有效工时来自"预计兜底"的工作项数
    total_hours = 0.0

    for i, it in enumerate(all_items):
        wid = it.get("identifier") or it.get("id")
        cat = it["_cat"]
        by_cat[cat]["items"] += 1
        recs = get_effort_records(args.base, args.org_id, headers, wid)
        actual = _sum_effort(recs)

        if by_owner_item:
            # ---- 工作项级归口：有效工时归到「任务负责人 assignedTo」----
            hours = actual
            from_est = False
            if hours <= 0 and use_est_fallback:
                est_recs = get_estimated_efforts(args.base, args.org_id, headers, wid)
                est = _sum_effort(est_recs)
                if est > 0:
                    hours = est
                    from_est = True
            if hours > 0:
                items_with_effort += 1
                by_cat[cat]["with_effort"] += 1
                by_cat[cat]["hours"] += hours
                total_hours += hours
                if from_est:
                    items_by_estimate += 1
                owner = (it.get("assignedTo") or {}).get("name") or "(未分配)"
                hours_by_person[owner] += hours
                records_by_person[owner] += 1
        else:
            # ---- 记录级归口：按每条实际工时记录的 creator/owner 聚合（预计工时无记录级归属，不兜底）----
            if recs:
                items_with_effort += 1
                by_cat[cat]["with_effort"] += 1
                for rec in recs:
                    at = rec.get("actualTime") or 0
                    total_hours += at
                    by_cat[cat]["hours"] += at
                    person = (rec.get(args.by) or {}).get("name") or "(未知)"
                    hours_by_person[person] += at
                    records_by_person[person] += 1

        if (i + 1) % 50 == 0:
            print(f"  进度 {i+1}/{len(all_items)} ...")
        time.sleep(0.08)

    fill_rate = round(items_with_effort / max(len(all_items), 1) * 100, 1)
    unit = "工作项" if by_owner_item else "条"
    print(f"\n{'='*55}")
    print(f"工作项总数: {len(all_items)}")
    print(f"有有效工时的工作项: {items_with_effort} (填报率 {fill_rate}%)"
          + (f"，其中 {items_by_estimate} 项来自预计工时兜底" if by_owner_item else ""))
    print(f"总有效工时: {total_hours:.1f} 小时，涉及人数: {len(hours_by_person)}")
    print(f"按{'任务负责人' if by_owner_item else args.by}:")
    for name, h in sorted(hours_by_person.items(), key=lambda x: -x[1]):
        print(f"  {name}: {h:.1f}h ({records_by_person[name]}{unit})")

    out = {
        "metadata": {
            "since": cutoff.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "until": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "days": args.days,
            "attribution": args.by,
            "attribution_level": "workitem" if by_owner_item else "record",
            "value_rule": "actual_first_estimate_fallback" if use_est_fallback else "actual_only",
            "categories": categories,
            "total_items": len(all_items),
            "items_with_effort": items_with_effort,
            "items_by_estimate": items_by_estimate,
            "fill_rate": fill_rate,
            "total_hours": round(total_hours, 1),
            "distinct_people": len(hours_by_person),
            "by_category": {c: {"items": d["items"], "with_effort": d["with_effort"],
                                "hours": round(d["hours"], 1)} for c, d in by_cat.items()},
        },
        "effort_by_person": {name: {"hours": round(h, 1), "records": records_by_person[name]}
                             for name, h in hours_by_person.items()},
    }
    path = os.path.join(args.workdir, "effort_data.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 已保存: {path}")
    if fill_rate < 40:
        print(f"⚠️ 填报率仅 {fill_rate}%(<40%)，compute_metrics 将自动剔除工时维度并分摊权重。")


if __name__ == "__main__":
    main()
