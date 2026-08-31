#!/usr/bin/env python3
"""
云效代码审计 - 生成 HTML 报告
把 enhanced_audit_data.json 注入 HTML 模板，产出 audit_report.html。

用法:
  python3 generate_report.py --workdir <workdir> --template <template.html> [--out audit_report.html]
"""
import argparse
import json
import os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', required=True)
    ap.add_argument('--template', required=True, help='HTML 模板路径（含 {/* DATA_PLACEHOLDER */}）')
    ap.add_argument('--data', default='enhanced_audit_data.json')
    ap.add_argument('--out', default='audit_report.html')
    args = ap.parse_args()

    data_path = os.path.join(args.workdir, args.data)
    out_path = os.path.join(args.workdir, args.out)

    with open(data_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    with open(args.template, 'r', encoding='utf-8') as f:
        template = f.read()

    html = template.replace('{/* DATA_PLACEHOLDER */}', json.dumps(data, ensure_ascii=False))
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"✅ HTML 报告已生成: {out_path}")


if __name__ == "__main__":
    main()
