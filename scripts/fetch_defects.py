#!/usr/bin/env python3
"""
云效代码审计 - 缺陷(Bug)数据采集
通过云效项目协作(projex)工作项 API 拉取缺陷，按「指派负责人(assignedTo)」聚合，
用于计算「缺陷密度 = 缺陷数 / 千有效行(KLOC)」质量维度。

缺陷归属口径：默认按 assignedTo（缺陷被指派给谁修复 = 谁对这块代码质量负责）。
可用 --by creator 改为按创建者统计（一般不用，创建者多为测试/产品）。

用法:
  python3 fetch_defects.py --token <PAT> --org-id <orgId> --days 30 --workdir <workdir>
  # 可选：--space-id <projectId> 指定项目；不填则自动列出组织下所有项目并全部纳入
  # 可选：--by assignedTo|creator  缺陷归属口径，默认 assignedTo

注意：缺陷统计窗口应与代码采集窗口(collect.py --days)保持一致，否则密度口径不对齐。
"""
import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests


def make_headers(token):
    return {"x-yunxiao-token": token, "Content-Type": "application/json"}


def list_projects(base, org_id, headers):
    """列出组织下所有项目(spaceId)。"""
    url = f"{base}/oapi/v1/projex/organizations/{org_id}/projects:search"
    projects, page = [], 1
    while True:
        r = requests.post(url, headers=headers, json={"page": page, "perPage": 100}, timeout=30)
        if r.status_code != 200:
            print(f"  ⚠️ 列项目 HTTP {r.status_code}: {r.text[:200]}")
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


def search_bugs(base, org_id, headers, space_id):
    """分页拉取某项目下所有缺陷。"""
    url = f"{base}/oapi/v1/projex/organizations/{org_id}/workitems:search"
    bugs, page = [], 1
    while True:
        body = {"category": "Bug", "spaceId": space_id, "spaceType": "Project",
                "orderBy": "gmtCreate", "page": page, "perPage": 100, "sort": "desc"}
        r = requests.post(url, headers=headers, json=body, timeout=30)
        if r.status_code != 200:
            print(f"  ⚠️ 搜缺陷 HTTP {r.status_code}: {r.text[:200]}")
            break
        data = r.json()
        items = data if isinstance(data, list) else (data.get("workitems") or data.get("result") or [])
        if not items:
            break
        bugs.extend(items)
        if len(items) < 100:
            break
        page += 1
        time.sleep(0.2)
    return bugs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True, help="云效 PAT（需 项目协作 只读权限）")
    ap.add_argument("--org-id", required=True)
    ap.add_argument("--base", default="https://openapi-rdc.aliyuncs.com")
    ap.add_argument("--days", type=int, default=30, help="回溯天数，默认30（须与 collect.py 一致）")
    ap.add_argument("--space-id", default=None, help="项目ID(spaceId)；不填则纳入组织下所有项目")
    ap.add_argument("--by", default="assignedTo", choices=["assignedTo", "creator"],
                    help="缺陷归属口径，默认按指派负责人 assignedTo")
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()
    os.makedirs(args.workdir, exist_ok=True)

    headers = make_headers(args.token)
    now = datetime.now(timezone.utc) + timedelta(hours=8)  # 北京时间
    cutoff = now - timedelta(days=args.days)

    print("=" * 60)
    print(f"云效缺陷采集 | 窗口: 最近{args.days}天 (>= {cutoff.strftime('%Y-%m-%d')}) | 归属: {args.by}")
    print("=" * 60)

    # 确定项目列表
    if args.space_id:
        space_ids = [args.space_id]
        proj_names = {args.space_id: args.space_id}
    else:
        projs = list_projects(args.base, args.org_id, headers)
        space_ids = [p["id"] for p in projs]
        proj_names = {p["id"]: p.get("name", p["id"]) for p in projs}
        print(f"发现 {len(space_ids)} 个项目: {[proj_names[s] for s in space_ids]}")

    all_bugs = []
    for sid in space_ids:
        bugs = search_bugs(args.base, args.org_id, headers, sid)
        print(f"  项目 {proj_names.get(sid, sid)}: {len(bugs)} 缺陷(全部历史)")
        all_bugs.extend(bugs)

    # 时间窗过滤 + 按归属人聚合
    by_person = defaultdict(lambda: {"count": 0, "serials": [], "open": 0, "closed": 0})
    kept = 0
    for b in all_bugs:
        gmt = b.get("gmtCreate")
        if not gmt:
            continue
        created = datetime.fromtimestamp(gmt / 1000, tz=timezone.utc) + timedelta(hours=8)
        if created < cutoff:
            continue
        owner = (b.get(args.by) or {}).get("name") or "(未指派)"
        by_person[owner]["count"] += 1
        by_person[owner]["serials"].append(b.get("serialNumber", ""))
        status_en = (b.get("status") or {}).get("nameEn", "")
        # 简单区分：已关闭/已修复 vs 未关闭
        if status_en.lower() in ("closed", "fixed", "resolved", "done"):
            by_person[owner]["closed"] += 1
        else:
            by_person[owner]["open"] += 1
        kept += 1

    print(f"\n窗口内缺陷: {kept} / 全部 {len(all_bugs)}")
    print("按归属人分布:")
    for name, d in sorted(by_person.items(), key=lambda x: -x[1]["count"]):
        print(f"  {name}: {d['count']} (未关闭 {d['open']}, 已关闭 {d['closed']})")

    out = {
        "metadata": {
            "since": cutoff.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "until": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "days": args.days,
            "attribution": args.by,
            "total_bugs_all": len(all_bugs),
            "total_bugs_window": kept,
        },
        "defects_by_person": {name: d for name, d in by_person.items()},
    }
    path = os.path.join(args.workdir, "defects_data.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 已保存: {path}")


if __name__ == "__main__":
    main()
