#!/usr/bin/env python3
"""Cloudmorph 플랫폼용 시크릿 스토어(secret://) 간편 등록 스크립트.

공개 저장소이므로 비밀번호에 기본값을 두지 않습니다. 값은 인자, 환경변수 순으로 읽고, 없으면 터미널에서 묻습니다.
터미널이 아니면 아무것도 저장하지 않고 오류로 끝납니다.

사용법:
  # 없는 값은 터미널에서 묻습니다 (gcloud 키는 기본 경로에서 찾습니다)
  python scripts/register_secrets.py

  # 환경변수: SOURCE_DB_PASSWORD, TARGET_DB_PASSWORD, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY

  # 특정 run_id 접두어로 등록 (예: secret://mig-1234/...)
  python scripts/register_secrets.py --run-id mig-1234
"""

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

# Add backend directory to path
backend_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(backend_dir))

from app.core.secrets import put_secret


def _ask(given: str | None, prompt: str) -> str | None:
    """받은 값이 없으면 터미널에서 묻습니다. 터미널이 아니거나 빈 입력이면 None 입니다."""
    if given:
        return given
    return (getpass.getpass(f"{prompt}: ") or None) if sys.stdin.isatty() else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Register encrypted secrets for Cloudmorph")
    parser.add_argument("--run-id", default="default", help="Prefix for secret reference, e.g. 'default' or a run_id")
    parser.add_argument("--aws-key", default=os.getenv("AWS_ACCESS_KEY_ID"), help="AWS Access Key ID")
    parser.add_argument("--aws-secret", default=os.getenv("AWS_SECRET_ACCESS_KEY"), help="AWS Secret Access Key")
    parser.add_argument("--db-user", default="postgres", help="Source PostgreSQL superuser (default: postgres)")
    parser.add_argument("--db-pass", default=os.getenv("SOURCE_DB_PASSWORD"), help="Source PostgreSQL password")
    parser.add_argument("--target-db-pass", default=os.getenv("TARGET_DB_PASSWORD"), help="Target Cloud SQL password")
    parser.add_argument(
        "--gcp-sa-key",
        default=os.getenv("GOOGLE_APPLICATION_CREDENTIALS", str(Path.home() / ".config/gcloud/cloudmorph-sa-key.json")),
        help="Path to GCP Service Account JSON key",
    )
    args = parser.parse_args()

    # 일부만 저장되지 않도록 값을 모두 모은 뒤에 저장합니다.
    db_pass = _ask(args.db_pass, "원본 DB 비밀번호 (SOURCE_DB_PASSWORD)")
    target_db_pass = _ask(args.target_db_pass, "대상 DB 비밀번호 (TARGET_DB_PASSWORD)")
    if not (db_pass and target_db_pass):
        sys.exit("[!] DB 비밀번호가 없습니다. SOURCE_DB_PASSWORD·TARGET_DB_PASSWORD 환경변수나 --db-pass·--target-db-pass 로 넘기세요.")
    aws_key = _ask(args.aws_key, "AWS Access Key ID (없으면 Enter)")
    aws_secret = _ask(args.aws_secret, "AWS Secret Access Key") if aws_key else args.aws_secret
    if bool(aws_key) != bool(aws_secret):
        sys.exit("[!] AWS 키는 AWS_ACCESS_KEY_ID 와 AWS_SECRET_ACCESS_KEY 를 함께 넘기세요.")

    run_id = args.run_id
    print(f"[*] Registering secrets with prefix: secret://{run_id}/...")

    # 1. AWS Cloud Credentials
    if aws_key:
        put_secret(f"{run_id}/source-cloud", {
            "aws_access_key_id": aws_key,
            "aws_secret_access_key": aws_secret,
        })
        print(f"  [+] Registered: secret://{run_id}/source-cloud")
    else:
        print(f"  [!] 안내: AWS 키가 없어 secret://{run_id}/source-cloud 를 등록하지 않습니다. 원본 AWS 조회와 VPN 에는 이 키가 필요합니다.")

    # 2. Source DB Admin
    put_secret(f"{run_id}/source-db-admin", {
        "user": args.db_user,
        "password": db_pass,
    })
    print(f"  [+] Registered: secret://{run_id}/source-db-admin")

    # 3. Target Cloud (GCP SA Key)
    sa_path = Path(args.gcp_sa_key)
    if sa_path.exists():
        with open(sa_path, encoding="utf-8") as f:
            sa_json = f.read()
    else:
        print(f"  [!] 안내: GCP 서비스 계정 키 파일({sa_path})이 없어 로컬 테스트용 더미 계정으로 등록합니다.")
        sa_json = json.dumps({"type": "service_account", "project_id": os.getenv("GCP_PROJECT_ID", "local-test")})
    put_secret(f"{run_id}/target-cloud", {"credentials_json": sa_json})
    print(f"  [+] Registered: secret://{run_id}/target-cloud")

    # 4. Target DB Admin
    put_secret(f"{run_id}/target-admin", {
        "user": "postgres",
        "password": target_db_pass,
    })
    print(f"  [+] Registered: secret://{run_id}/target-admin")

    print("\n✅ All secrets successfully encrypted and stored in local SQLite!")
    print("   References ready to paste in UI:")
    if aws_key:
        print(f"   - AWS 자격증명 참조   : secret://{run_id}/source-cloud (or profile:default)")
    print(f"   - DB 관리자 계정 참조 : secret://{run_id}/source-db-admin")


if __name__ == "__main__":
    main()
