"""AWS 로 보내는 문자열(보안 그룹 설명, 이름 등)은 ASCII 만 받는다.

한글 설명을 넣으면 plan 은 통과하고 apply 에서 InvalidParameterValue 로 실패한다.
(실제 사례: 보안 그룹 GroupDescription) 그래서 리소스 파일의 주석 아닌 줄에는 한글을 두지 않는다.
variables.tf / outputs.tf 의 description 은 Terraform 안에서만 쓰여 AWS 로 가지 않으므로 제외한다.
"""

from pathlib import Path

AWS_DIR = Path(__file__).resolve().parents[4] / "infra" / "terraform" / "aws"
NOT_SENT_TO_AWS = {"variables.tf", "outputs.tf", "output.tf"}


def test_resource_files_have_no_non_ascii_outside_comments() -> None:
    offenders: list[str] = []
    for path in sorted(AWS_DIR.rglob("*.tf")):
        if ".terraform" in path.parts or path.name in NOT_SENT_TO_AWS:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code: str = line.split("#", 1)[0]
            if any(ord(char) > 127 for char in code):
                offenders.append(f"{path.relative_to(AWS_DIR)}:{number}: {line.strip()}")
    assert not offenders, "AWS 가 거부하는 비 ASCII 문자열:\n" + "\n".join(offenders)
