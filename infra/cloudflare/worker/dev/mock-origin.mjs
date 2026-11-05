// 로컬 시험용 가짜 원본. 사용: node dev/mock-origin.mjs <이름> <포트>
// 응답 헤더 X-Served-By 에 이름을 담고, ?delay_ms=N 으로 느린 요청을 흉내 낼 수 있다.
import http from "node:http";

const [, , name = "aws", port = "9001"] = process.argv;

http
  .createServer((req, res) => {
    const delay = Number(new URL(req.url, "http://x").searchParams.get("delay_ms")) || 0;
    setTimeout(() => {
      res.writeHead(200, { "content-type": "application/json", "x-served-by": name });
      res.end(JSON.stringify({ served_by: name, path: req.url }));
    }, delay);
  })
  .listen(Number(port), "127.0.0.1", () => console.log(`mock origin ${name} on :${port}`));
