#!/usr/bin/env python3
"""Cloudmorph 플랫폼용 시크릿 스토어(secret://) 간편 등록 스크립트.

사용법:
  # 기본 등록 (AWS 프로필 및 gcloud 기본 경로 자동 탐색)
  python scripts/register_secrets.py

  # 특정 run_id 접두어로 등록 (예: secret://mig-1234/...)
  python scripts/register_secrets.py --run-id mig-1234
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Add backend directory to path
backend_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(backend_dir))

from app.core.secrets import put_secret


def main() -> None:
    parser = argparse.ArgumentParser(description="Register encrypted secrets for Cloudmorph")
    parser.add_argument("--run-id", default="default", help="Prefix for secret reference, e.g. 'default' or a run_id")
    parser.add_argument("--aws-key", default=os.getenv("AWS_ACCESS_KEY_ID"), help="AWS Access Key ID")
    parser.add_argument("--aws-secret", default=os.getenv("AWS_SECRET_ACCESS_KEY"), help="AWS Secret Access Key")
    parser.add_argument("--db-user", default="postgres", help="Source PostgreSQL superuser (default: postgres)")
    parser.add_argument("--db-pass", default="CloudmorphPass123!", help="Source PostgreSQL password")
    parser.add_argument("--target-db-pass", default="CloudmorphTargetPass456!", help="Target Cloud SQL password")
    parser.add_argument(
        "--gcp-sa-key",
        default=os.getenv("GOOGLE_APPLICATION_CREDENTIALS", str(Path.home() / ".config/gcloud/cloudmorph-sa-key.json")),
        help="Path to GCP Service Account JSON key",
    )
    args = parser.parse_args()

    run_id = args.run_id
    print(f"[*] Registering secrets with prefix: secret://{run_id}/...")

    # 1. AWS Cloud Credentials
    aws_key = args.aws_key or "AKIA_DUMMY_KEY_FOR_LOCAL_TEST"
    aws_secret = args.aws_secret or "DUMMY_SECRET_KEY_FOR_LOCAL_TEST"
    put_secret(f"{run_id}/source-cloud", {
        "aws_access_key_id": aws_key,
        "aws_secret_access_key": aws_secret,
    })
    print(f"  [+] Registered: secret://{run_id}/source-cloud")

    # 2. Source DB Admin
    put_secret(f"{run_id}/source-db-admin", {
        "user": args.db_user,
        "password": args.db_pass,
    })
    print(f"  [+] Registered: secret://{run_id}/source-db-admin")

    # 3. Target Cloud (GCP SA Key)
    sa_path = Path(args.gcp_sa_key)
    if sa_path.exists():
        with open(sa_path, encoding="utf-8") as f:
            sa_json = f.read()
    else:
        sa_json = json.dumps({"type": "service_account", "project_id": os.getenv("GCP_PROJECT_ID", "local-test")})
    put_secret(f"{run_id}/target-cloud", {"credentials_json": sa_json})
    print(f"  [+] Registered: secret://{run_id}/target-cloud")

    # 4. Target DB Admin
    put_secret(f"{run_id}/target-admin", {
        "user": "postgres",
        "password": args.target_db_pass,
    })
    print(f"  [+] Registered: secret://{run_id}/target-admin")

    print("\n✅ All secrets successfully encrypted and stored in local SQLite!")
    print("   References ready to paste in UI:")
    print(f"   - AWS 자격증명 참조   : secret://{run_id}/source-cloud (or profile:default)")
    print(f"   - DB 관리자 계정 참조 : secret://{run_id}/source-db-admin")


if __name__ == "__main__":
    main()
