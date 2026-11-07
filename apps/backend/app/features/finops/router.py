"""단독 실행·확인용 API. main.py 에 등록되어 있고, 아직 화면·워크플로 호출자는 없습니다."""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from . import service
from .schemas import FinopsInput, FinopsReport


class _NoEchoRoute(APIRoute):
    """422 본문에 입력값을 싣지 않습니다.

    FastAPI 기본 422 는 input 을 그대로 돌려주는데, Infinity·NaN 은 JSON 으로 못 바꿔 500 이 됩니다.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                detail = [{"type": e["type"], "loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
                return JSONResponse(status_code=422, content={"detail": detail})

        return handler


router = APIRouter(prefix="/finops", tags=["finops"], route_class=_NoEchoRoute)


@router.post("/report", response_model=FinopsReport)
def make_report(inp: FinopsInput) -> FinopsReport:
    """비용 계산 결과를 받아 절감 계산과 제안 문장을 돌려줍니다."""
    return service.run(inp)
