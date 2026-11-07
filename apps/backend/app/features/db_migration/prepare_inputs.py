"""Prepare the 001 request and secret-store values without starting a migration.

Run from apps/backend: python -m app.features.db_migration.prepare_inputs
Cloud API reads reuse cloud_lookup. Passwords are supplied, never recovered from GCP.
"""

import argparse
import getpass
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import warnings
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from app.core.config import data_dir
from app.core.secrets import put_secret, resolve

from . import cloud_lookup
from .run_input import CLOUD_CREDENTIAL_KEYS, InvalidInput, inject, parse
from .schemas import RUN_ID_PATTERN


class PreparationError(RuntimeError):
    """Safe message only: never include credentials or provider responses."""


def read_gcp_key(path: Path) -> dict[str, str]:
    try:
        raw = path.expanduser().read_text(encoding="utf-8")
        key = json.loads(raw)
        if (not isinstance(key, dict) or key.get("type") != "service_account"
                or not all(isinstance(key.get(k), str) and key[k].strip()
                           for k in ("project_id", "client_email", "private_key", "token_uri"))):
            raise ValueError
        # Do not send a private-key assertion to an arbitrary URL in an imported file.
        if key["token_uri"] != "https://oauth2.googleapis.com/token":
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise PreparationError("GCP 서비스 계정 키 파일을 읽을 수 없거나 형식이 잘못되었습니다.") from None
    return {"credentials_json": raw}


def stored_source(run_id: str | None) -> tuple[str | None, dict[str, str] | None]:
    """Select only an unambiguous saved account; never create a DB just to list it."""
    if run_id:
        # Validate before the identifier is used as a secret reference.
        if not re.fullmatch(RUN_ID_PATTERN, run_id):
            raise PreparationError("--source-run-id 형식이 잘못되었습니다.")
        ref = f"secret://{run_id}/source-db-admin"
    else:
        path = (data_dir() / "alpha.db").resolve()
        if not path.exists():
            return None, None
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as conn:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM secrets WHERE name LIKE '%/source-db-admin'"
            )]
        if len(names) > 1:
            raise PreparationError("저장된 원본 DB 계정이 여러 개입니다. --source-run-id를 지정하세요.")
        if not names:
            return None, None
        ref = "secret://" + names[0]
    return ref, resolve(ref)


def aws_credentials(source_ref: str | None, profile: str | None) -> dict[str, str]:
    if not profile:
        refs = ["secret://cloud/aws"]
        if source_ref:
            refs.append(source_ref.removesuffix("source-db-admin") + "source-cloud")
        for ref in refs:
            try:
                result = resolve(ref)
            except KeyError:
                continue
            return validate_aws(result)
    import boto3
    credentials = boto3.Session(profile_name=profile).get_credentials()
    if credentials is None:
        raise PreparationError("AWS 권한이 없습니다. 기존 저장소 또는 --aws-profile을 준비하세요.")
    frozen = credentials.get_frozen_credentials()
    if frozen.token:
        raise PreparationError("001 명세는 임시 AWS 권한을 허용하지 않습니다. 영구 IAM 키가 필요합니다.")
    return validate_aws({"aws_access_key_id": frozen.access_key, "aws_secret_access_key": frozen.secret_key})


def validate_aws(value: dict[str, str]) -> dict[str, str]:
    if value.keys() != CLOUD_CREDENTIAL_KEYS["aws"] or not all(
        isinstance(v, str) and v and v == v.strip() for v in value.values()
    ):
        raise PreparationError("저장된 AWS 권한이 001 형식과 다릅니다. 영구 IAM 키 두 개가 필요합니다.")
    return value


def gcp_region(credentials: dict[str, str], selected: str | None) -> str:
    if selected:
        if not cloud_lookup.REGION_PATTERNS["gcp"].fullmatch(selected):
            raise PreparationError("GCP 리전 형식이 잘못되었습니다.")
        return selected
    from urllib.parse import quote
    key = cloud_lookup._gcp_key(credentials)
    token = cloud_lookup._gcp_token(key)
    with cloud_lookup._http() as client:
        instances = [db for db in cloud_lookup._gcp_pages(
            client, f"{cloud_lookup.GCP_SQL}/projects/{quote(key['project_id'], safe='')}/instances",
            token, "Cloud SQL",
        ) if db.get("databaseVersion", "").startswith("POSTGRES")
            and db.get("instanceType", "CLOUD_SQL_INSTANCE") == "CLOUD_SQL_INSTANCE"]
    if len(instances) != 1 or not instances[0].get("region"):
        raise PreparationError("GCP primary PostgreSQL이 정확히 하나가 아닙니다. --target-region을 지정하세요.")
    return instances[0]["region"]


def lookup(cloud: str, region: str, credentials: dict[str, str]) -> cloud_lookup.FoundEndpoint:
    if not cloud_lookup.REGION_PATTERNS[cloud].fullmatch(region):
        raise PreparationError("클라우드 리전 형식이 잘못되었습니다.")
    # Keep supplied keys in memory until the operator explicitly requests registration.
    found = cloud_lookup._LOOKUPS[cloud](region, credentials)
    cloud_lookup._validate(found)
    return found


def build_bundle(run_id: str, source: cloud_lookup.FoundEndpoint, target: cloud_lookup.FoundEndpoint,
                 source_account: dict[str, str], target_account: dict[str, str], app_user: str,
                 aws: dict[str, str], gcp: dict[str, str]) -> dict:
    request = {
        "run_id": run_id,
        "source": {"cloud": source.cloud, "region": source.region, "db": source_account},
        "target": {"cloud": target.cloud, "region": target.region,
                   "db": {**target_account, "app_user": app_user}},
    }
    parse(request)  # Keep precisely the 001 input contract; no extra execution fields.
    return {
        "run_input": request,
        "secret_values": {
            "cloud/aws": validate_aws(aws), "cloud/gcp": gcp,
            f"{run_id}/source-db-admin": dict(source_account),
            f"{run_id}/target-admin": dict(target_account),
            f"{run_id}/run-clouds": {
                "source": source.cloud, "source_region": source.region,
                "target": target.cloud, "target_region": target.region,
            },
        },
        "discovered": {"source": asdict(source), "target": asdict(target)},
        "verification": {
            "cloud_resources": "queried",
            "db_passwords": "not_checked_requires_vpn_db_connection",
            "target_app_user": "supplied_not_checked",
            "source_app_uses_admin_account": "operator_must_confirm",
            "database_and_tables": "not_queried_requires_vpn",
            "vpn_and_start_checks": "not_run",
            "migration": "not_started",
        },
    }


def register_bundle(bundle: dict) -> None:
    """Use the existing encrypted store, not plaintext SQLite inserts or a new key."""
    request = parse(bundle["run_input"])
    for cloud in ("aws", "gcp"):
        put_secret(f"cloud/{cloud}", bundle["secret_values"][f"cloud/{cloud}"])
    inject(request.run_id, request)


def save_bundle(bundle: dict, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix="cdc-inputs-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(bundle, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name).resolve()


def copy_json(value: dict) -> None:
    commands = (["pbcopy"],) if sys.platform == "darwin" else (
        ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]
    )
    command = next((cmd for cmd in commands if shutil.which(cmd[0])), None)
    if command is None:
        raise PreparationError("클립보드 도구가 없습니다. 저장한 파일을 사용하세요.")
    try:
        subprocess.run(command, input=json.dumps(value, ensure_ascii=False, indent=2),
                       text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise PreparationError("클립보드 복사에 실패했습니다. 저장한 파일을 사용하세요.") from None


def clipboard_menu(bundle: dict) -> None:
    while True:
        choice = input("Enter: 001 JSON 복사 / d: DB 저장값 복사 / b: 전체 복사 / q: 종료 > ").strip().lower()
        if choice == "q":
            return
        keys = {"": "run_input", "d": "secret_values", "b": None}
        if choice not in keys:
            print("Enter, d, b, q 중 선택하세요.")
            continue
        key = keys[choice]
        copy_json(bundle if key is None else bundle[key])
        print("클립보드에 복사했습니다. 비밀번호가 포함되어 있습니다.")


def ask_password(prompt: str) -> str:
    # getpass normally falls back to echo on unsupported terminals; fail closed instead.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise PreparationError("숨김 입력이 가능한 터미널에서 실행하세요.") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="001 CDC 실제 입력 준비 · AWS → GCP · 이전 실행 없음")
    parser.add_argument("--gcp-key", type=Path, help="GCP 서비스 계정 JSON 파일")
    parser.add_argument("--source-run-id", help="재사용할 기존 원본 계정의 run_id (후보가 하나면 자동 선택)")
    parser.add_argument("--source-region", help="AWS 조회 리전. 생략하면 실행 중에 입력")
    parser.add_argument("--target-region", help="생략하면 프로젝트에서 유일한 Cloud SQL의 리전 조회")
    parser.add_argument("--source-user", help="저장된 원본 계정 대신 이 계정의 비밀번호를 숨김 입력")
    parser.add_argument("--target-user", default="postgres", help="목적지 관리자 (기본: postgres)")
    parser.add_argument("--target-app-user", default="alpha", help="기존 목적지 앱 계정 (기본: alpha, DB 확인 필요)")
    parser.add_argument("--aws-profile", help="저장된 AWS 권한 대신 사용할 영구 IAM 키 프로필")
    parser.add_argument("--run-id", default="mig-" + uuid4().hex[:12])
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[3] / ".data/cdc-inputs")
    parser.add_argument("--register", action="store_true", help="Backend와 같은 ALPHA_DATA_DIR/암호키로 실제 등록")
    parser.add_argument("--no-clipboard", action="store_true", help="파일 저장 후 종료")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        print("숨김 입력이 가능한 터미널에서 실행하세요.", file=sys.stderr)
        return 1
    try:
        print("AWS → GCP 입력 준비. API 조회만 하며 VPN·복제·전환은 실행하지 않습니다.")
        if args.source_user:
            if args.source_run_id and not re.fullmatch(RUN_ID_PATTERN, args.source_run_id):
                raise PreparationError("--source-run-id 형식이 잘못되었습니다.")
            ref = f"secret://{args.source_run_id}/source-db-admin" if args.source_run_id else None
            source_account = {"user": args.source_user, "password": ask_password("원본 DB 비밀번호: ")}
        else:
            ref, source_account = stored_source(args.source_run_id)
            if ref:
                print(f"원본 계정 재사용: {ref}")
        if source_account is None:
            raise PreparationError("저장된 원본 DB 계정이 없습니다. --source-user로 계정과 비밀번호를 입력하세요.")
        aws = aws_credentials(ref, args.aws_profile)
        source_region = args.source_region or input("AWS RDS 리전: ").strip()
        path = args.gcp_key or Path(input("GCP 서비스 계정 JSON 파일 경로: ").strip())
        gcp = read_gcp_key(path)
        target_account = {"user": args.target_user, "password": ask_password("목적지 DB 관리자 비밀번호: ")}
        # Validate password/name fields before any network reads. The real GCP region is looked up below.
        parse({"run_id": args.run_id,
               "source": {"cloud": "aws", "region": source_region, "db": source_account},
               "target": {"cloud": "gcp", "region": args.target_region or "region-placeholder1",
                          "db": {**target_account, "app_user": args.target_app_user}}})
        print("AWS RDS·GCP Cloud SQL 및 네트워크 실제 값 조회 중…")
        source = lookup("aws", source_region, aws)
        target = lookup("gcp", gcp_region(gcp, args.target_region), gcp)
        bundle = build_bundle(args.run_id, source, target, source_account, target_account,
                              args.target_app_user, aws, gcp)
        output = save_bundle(bundle, args.output_dir)
        print(f"전체 값 파일 (소유자만 읽기·쓰기): {output}")
        print(f"조회 완료: {source.cloud}/{source.region} → {target.cloud}/{target.region}")
        print("DB 비밀번호·앱 계정·DB/테이블·VPN·시작 조건은 미검증입니다.")
        if args.register:
            register_bundle(bundle)
            print("Backend 암호화 저장소에 클라우드 권한과 실행별 DB 계정을 등록했습니다.")
        else:
            print("DB 등록 전입니다. 실제 등록은 --register 옵션을 사용하세요.")
        if not args.no_clipboard:
            clipboard_menu(bundle)
        return 0
    except InvalidInput as exc:
        print("입력 형식 오류: " + ", ".join(exc.fields), file=sys.stderr)
    except cloud_lookup.LookupFailed as exc:
        # cloud_lookup deliberately exposes only fixed messages and HTTP status codes.
        print("클라우드 조회 실패: " + str(exc), file=sys.stderr)
    except PreparationError as exc:
        print(str(exc), file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print("입력을 취소했습니다.", file=sys.stderr)
    except Exception:
        # Provider, keychain, file and validation errors may contain submitted secrets.
        print("준비 실패: 권한·저장소 암호키·네트워크·리전 및 파일 접근을 확인하세요. 원문 오류는 출력하지 않습니다.",
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
