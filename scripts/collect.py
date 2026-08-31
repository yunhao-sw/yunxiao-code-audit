#!/usr/bin/env python3
"""
云效代码审计 - 数据采集 + 有效行数计算
从 Yunxiao Codeup API 拉取所有活跃仓库、所有分支的提交（按 SHA 去重），
剔除 merge commit，逐个 commit 通过 compare API 拉 diff，解析有效增删行数，
按 Git 身份聚合。产出 audit_data.json。

用法:
  python3 collect.py --token <PAT> --org-id <orgId> --days 30 --out <workdir>
"""
import argparse
import requests
import json
import time
import re
import os
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# === 需求 ID 正则：按你团队的前缀自定义（如 JSTTZX-1234 / FEAT-12 / BUG-5）===
NEED_ID_RE = re.compile(r'([A-Z]{2,}-\d+)', re.IGNORECASE)

# === 排除出"有效行数"的文件模式 ===
EXCLUDE_FILE_PATTERNS = [
    r'package-lock\.json$', r'yarn\.lock$', r'pnpm-lock\.yaml$', r'Gemfile\.lock$',
    r'pipfile\.lock$', r'composer\.lock$', r'poetry\.lock$',
    r'\.md$', r'\.markdown$', r'\.rst$', r'\.adoc$',  # 文档类，不计入工作量
    r'\.min\.js$', r'\.min\.css$', r'\.map$', r'\.bundle\.js$',
    r'^dist/', r'^build/', r'^\.next/', r'^out/', r'^target/', r'^bin/', r'^node_modules/',
    r'\.jar$', r'\.war$', r'\.class$', r'\.pyc$', r'\.pyo$', r'\.o$', r'\.so$', r'\.dll$', r'\.exe$',
    r'\.png$', r'\.jpg$', r'\.jpeg$', r'\.gif$', r'\.bmp$', r'\.ico$', r'\.tif$', r'\.tiff$',
    r'\.webp$', r'\.avif$', r'\.heic$',
    r'\.woff$', r'\.woff2$', r'\.ttf$', r'\.eot$', r'\.otf$',
    r'\.pdf$', r'\.doc$', r'\.docx$', r'\.xls$', r'\.xlsx$', r'\.ppt$', r'\.pptx$',
    r'\.zip$', r'\.tar$', r'\.gz$', r'\.rar$', r'\.7z$',
    r'\.mp3$', r'\.mp4$', r'\.wav$', r'\.avi$', r'\.mov$', r'\.flv$',
    r'\.checksum$', r'\.hash$', r'\.sig$',
    r'^\.idea/', r'^\.vscode/', r'\.DS_Store$',
]
EXCLUDE_FILE_RE = re.compile('|'.join(EXCLUDE_FILE_PATTERNS), re.IGNORECASE)
WHITESPACE_ONLY_RE = re.compile(r'^[\s\r\n]*$')

REQUEST_DELAY = 0.3
_last_request_time = 0


def make_headers(token):
    return {"x-yunxiao-token": token, "Content-Type": "application/json"}


def throttled_request(url, headers, params=None, max_retries=3):
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)
    _last_request_time = time.time()
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                print("  Rate limited, waiting 5s...")
                time.sleep(5)
                continue
            elif resp.status_code == 403:
                print(f"  403 Forbidden (token 可能缺少 Codeup 读权限): {resp.text[:200]}")
                return None
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:200]}")
                if attempt < max_retries - 1:
                    time.sleep(2); continue
                return None
        except requests.exceptions.RequestException as e:
            print(f"  Request error: {e}")
            if attempt < max_retries - 1:
                time.sleep(2); continue
            return None
    return None


def list_all_repos(base, org_id, headers):
    repos, page = [], 1
    while True:
        data = throttled_request(f"{base}/oapi/v1/codeup/organizations/{org_id}/repositories",
                                 headers, {"perPage": 100, "page": page})
        if not data or not isinstance(data, list):
            break
        repos.extend(data)
        if len(data) < 100:
            break
        page += 1
    return repos


def list_branches(base, org_id, repo_id, headers):
    branches, page = [], 1
    while True:
        data = throttled_request(f"{base}/oapi/v1/codeup/organizations/{org_id}/repositories/{repo_id}/branches",
                                 headers, {"perPage": 100, "page": page, "sort": "updated_desc"})
        if not data or not isinstance(data, list):
            break
        branches.extend(data)
        if len(data) < 100:
            break
        page += 1
    return branches


def list_commits(base, org_id, repo_id, ref_name, since, until, headers):
    commits, page = [], 1
    while True:
        data = throttled_request(f"{base}/oapi/v1/codeup/organizations/{org_id}/repositories/{repo_id}/commits",
                                 headers, {"refName": ref_name, "since": since, "until": until,
                                           "perPage": 100, "page": page})
        if not data or not isinstance(data, list):
            break
        commits.extend(data)
        if len(data) < 100:
            break
        page += 1
    return commits


def get_compare(base, org_id, repo_id, from_sha, to_sha, headers):
    return throttled_request(f"{base}/oapi/v1/codeup/organizations/{org_id}/repositories/{repo_id}/compares",
                             headers, {"from": from_sha, "to": to_sha})


def parse_diff_count_effective_lines(diff_text, file_path):
    if not diff_text:
        return (0, 0)
    if EXCLUDE_FILE_RE.search(file_path):
        return (0, 0)
    if 'Binary files differ' in diff_text or not diff_text.strip():
        return (0, 0)
    additions = deletions = 0
    for line in diff_text.split('\n'):
        if line.startswith('---') or line.startswith('+++') or line.startswith('@@') or line.startswith('\\'):
            continue
        if line.startswith('+'):
            if WHITESPACE_ONLY_RE.match(line[1:].strip()):
                continue
            additions += 1
        elif line.startswith('-'):
            if WHITESPACE_ONLY_RE.match(line[1:].strip()):
                continue
            deletions += 1
    return (additions, deletions)


def collect(base, org_id, token, since, until, out_dir):
    headers = make_headers(token)
    print("=" * 60)
    print(f"云效代码审计 - 数据采集\n时间范围: {since} → {until}")
    print("=" * 60)

    print("\n[1] 列出所有仓库...")
    repos = list_all_repos(base, org_id, headers)
    print(f"  共 {len(repos)} 个仓库")

    print("\n[2] 检测默认分支活跃度...")
    active_repos = []
    for repo in repos:
        repo_id = str(repo.get('id', ''))
        repo_name = repo.get('name', 'unknown')
        for ref in ['master', 'main']:
            commits = list_commits(base, org_id, repo_id, ref, since, until, headers)
            if commits:
                active_repos.append({'id': repo_id, 'name': repo_name,
                                     'description': repo.get('description', ''),
                                     'default_ref': ref, 'initial_commit_count': len(commits)})
                print(f"  ✅ {repo_name}: {len(commits)} commits on {ref}")
                break
        else:
            print(f"  ⬜ {repo_name}: 无近期提交")
    print(f"\n  活跃仓库: {len(active_repos)}/{len(repos)}")

    print("\n[3] 拉取所有分支提交（SHA 去重）...")
    all_commits, commit_repo_map = {}, {}
    for ri in active_repos:
        repo_id, repo_name = ri['id'], ri['name']
        branches = list_branches(base, org_id, repo_id, headers)
        new_c = 0
        for br in branches:
            bn = br.get('name', '')
            if not bn:
                continue
            for c in list_commits(base, org_id, repo_id, bn, since, until, headers):
                sha = c.get('id', '')
                if sha and sha not in all_commits:
                    all_commits[sha] = c
                    commit_repo_map[sha] = [repo_id]
                    new_c += 1
                elif sha and repo_id not in commit_repo_map.get(sha, []):
                    commit_repo_map[sha].append(repo_id)
        print(f"  {repo_name}: {len(branches)} 分支, 新增 {new_c} 提交")
    print(f"\n  唯一提交总数: {len(all_commits)}")

    print("\n[4] 剔除 merge commit...")
    merge_c = no_parent = 0
    non_merge = []
    for sha, c in all_commits.items():
        pids = c.get('parentIds', [])
        if not pids:
            no_parent += 1; continue
        if len(pids) >= 2:
            merge_c += 1; continue
        non_merge.append((sha, c))
    print(f"  merge: {merge_c}, 无父: {no_parent}, 待分析: {len(non_merge)}")

    print("\n[5] 逐 commit 拉 diff 计算有效行数（慢）...")
    dev = defaultdict(lambda: {'commits': 0, 'effective_additions': 0, 'effective_deletions': 0,
                               'repos': set(), 'active_days': set(), 'needs': set(), 'merge_commits': 0})
    processed = failed = 0
    for idx, (sha, c) in enumerate(non_merge):
        repo_id = commit_repo_map[sha][0]
        parent_sha = c.get('parentIds', [''])[0]
        name = c.get('authorName', 'unknown')
        email = c.get('authorEmail', 'unknown@unknown')
        cdate = c.get('committedDate', '')
        msg = c.get('message', '')
        need = NEED_ID_RE.search(msg)
        day = ''
        if cdate:
            try:
                day = datetime.fromisoformat(cdate.replace('Z', '+00:00')).strftime('%Y-%m-%d')
            except Exception:
                pass
        key = f"{name} <{email}>"
        dev[key]['commits'] += 1
        dev[key]['repos'].add(repo_id)
        if day:
            dev[key]['active_days'].add(day)
        if need:
            dev[key]['needs'].add(need.group(1))
        cmp_data = get_compare(base, org_id, repo_id, parent_sha, sha, headers)
        if cmp_data and 'diffs' in cmp_data:
            fa = fd = 0
            for d in cmp_data.get('diffs', []):
                fp = d.get('newPath', d.get('new_path', d.get('path', '')))
                dt = d.get('diff', d.get('patch', d.get('text', '')))
                if d.get('binary', False):
                    continue
                if d.get('renamedFile', False) and not dt:
                    continue
                a, dd = parse_diff_count_effective_lines(dt, fp)
                fa += a; fd += dd
            dev[key]['effective_additions'] += fa
            dev[key]['effective_deletions'] += fd
        else:
            failed += 1
        processed += 1
        if (idx + 1) % 20 == 0:
            print(f"    进度: {idx + 1}/{len(non_merge)}")
    print(f"\n  已处理: {processed}, 失败: {failed}")

    print("\n[6] 身份归一化（按邮箱小写）...")
    groups = defaultdict(list)
    for key in dev:
        m = re.match(r'^(.*?)\s*<(.+?)>$', key)
        if m:
            groups[m.group(2).strip().lower()].append((key, m.group(1).strip(), m.group(2).strip()))
        else:
            groups[key.lower()].append((key, key, ''))
    norm = {}
    for nk, entries in groups.items():
        merged = {'commits': 0, 'effective_additions': 0, 'effective_deletions': 0,
                  'repos': set(), 'active_days': set(), 'needs': set(), 'merge_commits': 0, 'raw_ids': []}
        name_counts = defaultdict(int)
        for key, nm, em in entries:
            for f in ('commits', 'effective_additions', 'effective_deletions'):
                merged[f] += dev[key][f]
            merged['repos'].update(dev[key]['repos'])
            merged['active_days'].update(dev[key]['active_days'])
            merged['needs'].update(dev[key]['needs'])
            merged['raw_ids'].append(key)
            name_counts[nm] += 1
        merged['display_name'] = max(name_counts.items(), key=lambda x: x[1])[0] if name_counts else nk
        merged['email'] = entries[0][2] or nk
        norm[nk] = merged
    print(f"  归一化前 {len(dev)} → 后 {len(norm)}")

    # merge commit 计数
    merge_by = defaultdict(int)
    for sha, c in all_commits.items():
        if len(c.get('parentIds', [])) >= 2:
            em = c.get('authorEmail', 'unknown@unknown')
            merge_by[em.lower()] += 1
    for nk in norm:
        norm[nk]['merge_commits'] = merge_by.get(nk, 0)

    print("\n[7] 保存 audit_data.json...")
    out = {
        'metadata': {'since': since, 'until': until, 'total_repos': len(repos),
                     'active_repos': len(active_repos), 'total_commits': len(all_commits),
                     'merge_commits_skipped': merge_c, 'non_merge_commits': len(non_merge),
                     'commits_processed': processed, 'commits_failed': failed,
                     'unique_developers': len(norm)},
        'repos': [{'id': r['id'], 'name': r['name'], 'description': r.get('description', '')} for r in repos],
        'active_repos': [{'id': r['id'], 'name': r['name'], 'description': r.get('description', ''),
                          'default_ref': r['default_ref'], 'commit_count': r['initial_commit_count']}
                         for r in active_repos],
        'developer_stats': {}, 'identity_map': {},
    }
    for nk, s in norm.items():
        out['developer_stats'][nk] = {
            'display_name': s['display_name'], 'email': s['email'], 'commits': s['commits'],
            'merge_commits': s['merge_commits'], 'effective_additions': s['effective_additions'],
            'effective_deletions': s['effective_deletions'],
            'net_effective': s['effective_additions'] - s['effective_deletions'],
            'repos': list(s['repos']), 'active_days': sorted(list(s['active_days'])),
            'needs': list(s['needs']), 'raw_ids': s['raw_ids'],
        }
        out['identity_map'][nk] = s['raw_ids']
    path = os.path.join(out_dir, 'audit_data.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"  已保存: {path}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument('--token', required=True, help='云效 PAT（需 Codeup 读权限）')
    ap.add_argument('--org-id', required=True)
    ap.add_argument('--base', default='https://openapi-rdc.aliyuncs.com')
    ap.add_argument('--days', type=int, default=30, help='回溯天数，默认30（最近一个月）')
    ap.add_argument('--out', required=True, help='输出目录')
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    collect(args.base, args.org_id, args.token, since, until, args.out)
    print("\n✅ 采集完成")
