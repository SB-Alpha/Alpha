"""클라우드마다 달라지는 안내 문구. 어느 줄을 쓸지는 초기 입력의 cloud 값으로만 정해진다."""

from dataclasses import dataclass

CREDENTIAL_NEXT_STEP = (
    "{console}에서 이번 이전용으로 만든 {identity}를 더 쓰지 않으면 삭제하세요. "
    "이 앱에 저장된 {cloud} 권한은 다른 이전에서도 쓰므로 지우지 않았습니다."
)


@dataclass(frozen=True)
class CloudGuide:
    console: str
    identity: str


CLOUD_GUIDES: dict[str, CloudGuide] = {
    "aws": CloudGuide(console="AWS 콘솔", identity="IAM 사용자"),
    "gcp": CloudGuide(console="Google Cloud 콘솔", identity="서비스 계정"),
    "azure": CloudGuide(console="Azure Portal", identity="서비스 주체"),
}


def credential_next_steps(*clouds: str) -> list[str]:
    """입력에 나온 클라우드마다 한 줄. 같은 클라우드는 한 번만 안내한다."""
    return [
        CREDENTIAL_NEXT_STEP.format(console=CLOUD_GUIDES[cloud].console, identity=CLOUD_GUIDES[cloud].identity, cloud=cloud)
        for cloud in dict.fromkeys(clouds)
    ]
