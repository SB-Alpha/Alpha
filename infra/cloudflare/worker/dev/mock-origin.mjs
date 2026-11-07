// 로컬 시험용 가짜 원본. 사용: node dev/mock-origin.mjs <이름> <포트>
// 응답 헤더 X-Served-By 에 이름을 담고, ?delay_ms=N 으로 느린 요청을, ?status=N 으로 오류 응답을 흉내 낼 수 있다.
import http from "node:http";

const [, , name = "aws", port = "9001"] = process.argv;

http
  .createServer((req, res) => {
    const params = new URL(req.url, "http://x").searchParams;
    const delay = Number(params.get("delay_ms")) || 0;
    const status = Number(params.get("status")) || 200;
    req.resume(); // 본문이 있는 요청도 응답할 수 있게 비운다
    setTimeout(() => {
      res.writeHead(status, { "content-type": "application/json", "x-served-by": name });
      res.end(JSON.stringify({ served_by: name, path: req.url }));
    }, delay);
  })
  .listen(Number(port), "127.0.0.1", () => console.log(`mock origin ${name} on :${port}`));
