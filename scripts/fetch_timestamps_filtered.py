#!/usr/bin/env python3
"""
云效代码审计 - 拉取 commit 时间戳（仓库过滤版）
读取 <workdir>/audit_data.json 获取时间范围和活跃仓库（已过滤），
只拉这些仓库的 commit 元数据用于时间/趋势维度。
"""
import argparse
import json
import time
import os
import requests

REQUEST_DELAY = 0.15
MAX_RETRIES = 4


def throttled_request(url, headers, params=None):
    time.sleep(REQUEST_DELAY)
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code != 200:
                print(f"  ⚠️ HTTP {resp.status_code}: {url}")
                return None
            return resp.json()
        except requests.exceptions.RequestException as e:
            last_err = e
            wait = 1.5 * (2 ** attempt)
            print(f"  ⚠️ 网络异常({type(e).__name__})，{wait:.0f}s 后重试 ({attempt+1}/{MAX_RETRIES})")
            time.sleep(wait)
    print(f"  ❌ 重试 {MAX_RETRIES} 次仍失败: {url} — {last_err}")
    return None


def list_branches(base, org_id, repo_id, headers):
    data = throttled_request(f"{base}/oapi/v1/codeup/organizations/{org_id}/repositories/{repo_id}/branches",
                             headers, {"perPage": 100})
    return data if isinstance(data, list) else []


def list_commits(base, org_id, repo_id, branch, since, until, headers):
    commits, page = [], 1
    while True:
        data = throttled_request(f"{base}/oapi/v1/codeup/organizations/{org_id}/repositories/{repo_id}/commits",
                                 headers, {"refName": branch, "since": since, "until": until,
                                           "perPage": 100, "page": page})
        if not data or not isinstance(data, list):
            break
        commits.extend(data)
        if len(data) < 100:
            break
        page += 1
    return commits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--token', required=True)
    ap.add_argument('--org-id', required=True)
    ap.add_argument('--base', default='https://openapi-rdc.aliyuncs.com')
    ap.add_argument('--workdir', required=True)
    args = ap.parse_args()

    headers = {"x-yunxiao-token": args.token, "Content-Type": "application/json"}
    with open(os.path.join(args.workdir, "audit_data.json"), 'r') as f:
        existing = json.load(f)

    since = existing["metadata"]["since"]
    until = existing["metadata"]["until"]
    repo_name_map = {int(r["id"]): r["name"] for r in existing["active_repos"]}

    print(f"拉取 {len(repo_name_map)} 个活跃仓库（已过滤）的 commit 时间戳...")
    all_commits = {}
    for repo_id, repo_name in repo_name_map.items():
        print(f"\n📦 {repo_name} (id={repo_id})")
        branches = list_branches(args.base, args.org_id, repo_id, headers)
        print(f"  分支: {len(branches)}")
        for br in branches:
            bn = br.get("name", "")
            for c in list_commits(args.base, args.org_id, repo_id, bn, since, until, headers):
                sha = c.get("id", "")
                if sha in all_commits:
                    continue
                if len(c.get("parentIds", [])) >= 2:
                    continue
                all_commits[sha] = {
                    "sha": sha, "author_name": c.get("authorName", ""),
                    "author_email": c.get("authorEmail", ""),
                    "committed_date": c.get("committedDate", ""),
                    "created_at": c.get("createdAt", ""), "message": c.get("message", ""),
                    "parent_count": len(c.get("parentIds", [])),
                    "repo_id": str(repo_id), "repo_name": repo_name, "branch": bn,
                }
        print(f"  累计非 merge 提交: {len(all_commits)}")

    print(f"\n✅ 收集 {len(all_commits)} 个 commit 时间戳")
    out = {"metadata": existing["metadata"], "commits": list(all_commits.values())}
    path = os.path.join(args.workdir, "commit_timestamps.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"已保存: {path}")


if __name__ == "__main__":
    main()
