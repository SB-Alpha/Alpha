"""원본을 실제로 조회해 AssessmentOutput 을 출력합니다 (로컬 확인 · fixture 만들기용).

    python -m app.features.assessment.cli --profile <읽기 전용 프로필> --aws-region ap-northeast-2 \
        [--ecs-cluster ...] [--ecs-service ...] [--out tests/assessment/fixtures/output.real.json]

레포가 공개라서 기본으로 계정 ID · VPC/서브넷/보안 그룹/라우팅 테이블 ID · RDS 주소를 가립니다.
가리지 않은 원본이 필요하면 --no-mask (파일로 저장하지 말고 화면으로만 보세요).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from .aws import ALLOW_PROFILE_ENV
from .schemas import AssessmentInput
from .service import run

AWS_IDS = re.compile(r"\b(vpc|subnet|sg|rtb|eni|igw)-[0-9a-f]{8,17}\b")
RDS_HOST = re.compile(r"\b[a-z0-9-]+\.[a-z0-9]+\.[a-z0-9-]+\.rds\.amazonaws\.com\b")


def mask(text: str, account_id: str | None) -> str:
    if account_id:
        text = text.replace(account_id, "<account>")
    text = re.sub(r"\b\d{12}\b", "<account>", text)
    text = AWS_IDS.sub(lambda m: f"{m.group(1)}-<masked>", text)
    return RDS_HOST.sub("<rds-host>", text)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="assessment 실제 원본 조회")
    p.add_argument("--profile", required=True, help="로컬 AWS CLI 프로필 (읽기 전용 계정)")
    p.add_argument("--aws-region", required=True)
    p.add_argument("--target-region", default="asia-northeast3")
    p.add_argument("--github-repo-url")
    p.add_argument("--ecs-cluster")
    p.add_argument("--ecs-service")
    p.add_argument("--rds-instance-id")
    p.add_argument("--out", help="저장할 JSON 경로. 없으면 화면 출력")
    p.add_argument("--no-mask", action="store_true", help="가리지 않음 (공개 레포에 커밋 금지)")
    args = p.parse_args(argv)

    if args.no_mask and args.out:
        p.error("--no-mask 결과는 파일로 저장하지 않습니다 (공개 레포 보호)")

    os.environ.setdefault(ALLOW_PROFILE_ENV, "1")  # 로컬 CLI 에서만 profile: 허용
    inp = AssessmentInput(
        aws_credentials_ref=f"profile:{args.profile}",
        aws_region=args.aws_region,
        target_region=args.target_region,
        github_repo_url=args.github_repo_url,
        ecs_cluster=args.ecs_cluster,
        ecs_service=args.ecs_service,
        rds_instance_id=args.rds_instance_id,
    )
    out = run(inp)
    data = out.model_dump(mode="json", exclude={"plan": {"generated_at"}})
    # 로컬 프로필 이름은 fixture 에 남기지 않음
    data["source"]["credentials_ref"]["cloud"] = "secret://<run>/aws"
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if not args.no_mask:
        text = mask(text, out.source.account_id)

    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
        print(f"저장: {args.out} (approvable={out.plan.approvable}, gaps={len(out.gaps)})", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
