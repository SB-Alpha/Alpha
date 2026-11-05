from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .target import TargetBuildError, build_target, find_problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="terraform output -json → provisioning target")
    parser.add_argument("--run-id", required=True, help="이 이전 작업의 run_id (비밀 이름표에 들어감)")
    parser.add_argument("--project-id", required=True, help="목적지 GCP 프로젝트 ID")
    parser.add_argument("--region", default="asia-northeast3", help="목적지 리전")
    parser.add_argument("--file", help="terraform output -json 을 저장한 파일. 없으면 표준 입력")
    args = parser.parse_args(argv)

    raw_text = Path(args.file).read_text() if args.file else sys.stdin.read()
    try:
        raw = json.loads(raw_text)
        target = build_target(raw, run_id=args.run_id, project_id=args.project_id, region=args.region)
    except (json.JSONDecodeError, TargetBuildError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 1

    for problem in find_problems(target):
        print(f"경고: {problem}", file=sys.stderr)
    print(json.dumps(target, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
