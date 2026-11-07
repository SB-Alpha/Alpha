"use client";

import { ContactShadows, Edges, Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import * as CANNON from "cannon-es";
import { useEffect, useRef } from "react";
import * as THREE from "three";
import { extractSourceNodeData, extractTargetNodeData } from "@/lib/topology3d";
import type { RunStatus } from "@/lib/workflows";

/**
 * 화면에 그리는 게이트 상태. 실시간 Worker mode/next_origin 이나 실측 cutover_steps 재생에서만 옵니다.
 * - source: 원본 처리 · hold: 요청 보류 · switch: 보류 중 목적지로 경로 변경됨 · target: 목적지 처리
 * - failed: 재생한 기록이 실패로 끝남 · unknown: 상태를 모름 (밸브 중립)
 */
export type CutoverPhase = "source" | "hold" | "switch" | "target" | "failed" | "unknown";

interface Props {
  run: RunStatus;
  phase: CutoverPhase;
  /** 실측 요청 비율에서 나온 초당 구슬 수. null 이거나 0 이면 구슬을 만들지 않습니다. */
  ballsPerSec: number | null;
}

const POOL_SIZE = 48;
const SPHERE_RADIUS = 0.11;
// 난수 대신 정해진 자리를 차례로 씁니다. 구슬 위치는 의미 없는 배치일 뿐입니다.
const SPAWN_OFFSETS: [number, number][] = [
  [-0.18, -0.12],
  [0.12, 0.1],
  [-0.05, 0.14],
  [0.2, -0.08],
  [-0.2, 0.05],
  [0.04, -0.15],
];

export function CutoverPhysicsScene({ run, phase, ballsPerSec }: Props) {
  const source = extractSourceNodeData(run);
  const target = extractTargetNodeData(run);

  const worldRef = useRef<CANNON.World | null>(null);
  const valveBodyRef = useRef<CANNON.Body | null>(null);
  const valveMeshRef = useRef<THREE.Mesh | null>(null);
  const holdGateBodyRef = useRef<CANNON.Body | null>(null);
  const holdGateMeshRef = useRef<THREE.Mesh | null>(null);

  interface SphereItem {
    body: CANNON.Body;
    mesh: THREE.Mesh | null;
    active: boolean;
    bornAt: number;
  }
  const spheresRef = useRef<SphereItem[]>([]);
  const spawnTimerRef = useRef(0);
  const spawnIndexRef = useRef(0);

  useEffect(() => {
    const world = new CANNON.World();
    world.gravity.set(0, -9.82, 0);
    world.defaultContactMaterial.friction = 0.12;
    world.defaultContactMaterial.restitution = 0.28;
    worldRef.current = world;

    // 1. 지면 바닥 (Static)
    const groundBody = new CANNON.Body({
      type: CANNON.Body.STATIC,
      shape: new CANNON.Box(new CANNON.Vec3(6, 0.1, 3)),
      position: new CANNON.Vec3(0, -1.75, 0),
    });
    world.addBody(groundBody);

    // 2. 이탈 방지용 투명 벽 (Invisible Walls - Project 3 Ch.2)
    const frontWall = new CANNON.Body({
      type: CANNON.Body.STATIC,
      shape: new CANNON.Box(new CANNON.Vec3(6, 4.5, 0.1)),
      position: new CANNON.Vec3(0, 1.0, 0.85),
    });
    const backWall = new CANNON.Body({
      type: CANNON.Body.STATIC,
      shape: new CANNON.Box(new CANNON.Vec3(6, 4.5, 0.1)),
      position: new CANNON.Vec3(0, 1.0, -0.85),
    });
    world.addBody(frontWall);
    world.addBody(backWall);

    // 3. 상단 Cloudflare Worker 보류 버퍼 슈트 (좌/우 벽 Static)
    const chuteLeft = new CANNON.Body({
      type: CANNON.Body.STATIC,
      shape: new CANNON.Box(new CANNON.Vec3(0.08, 0.65, 0.7)),
      position: new CANNON.Vec3(-0.55, 2.35, 0),
    });
    const chuteRight = new CANNON.Body({
      type: CANNON.Body.STATIC,
      shape: new CANNON.Box(new CANNON.Vec3(0.08, 0.65, 0.7)),
      position: new CANNON.Vec3(0.55, 2.35, 0),
    });
    world.addBody(chuteLeft);
    world.addBody(chuteRight);

    // 4. Cloudflare 요청 보류 댐 게이트 (Kinematic)
    // 열림: x = -1.25 / 닫힘(보류 중): x = 0
    const holdGateBody = new CANNON.Body({
      type: CANNON.Body.KINEMATIC,
      shape: new CANNON.Box(new CANNON.Vec3(0.52, 0.07, 0.7)),
      position: new CANNON.Vec3(-1.25, 1.72, 0),
    });
    world.addBody(holdGateBody);
    holdGateBodyRef.current = holdGateBody;

    // 5. 중앙 라우팅 전환 밸브 (Kinematic)
    const valveBody = new CANNON.Body({
      type: CANNON.Body.KINEMATIC,
      shape: new CANNON.Box(new CANNON.Vec3(1.25, 0.08, 0.75)),
      position: new CANNON.Vec3(0, 0.65, 0),
    });
    world.addBody(valveBody);
    valveBodyRef.current = valveBody;

    // 6. 좌측 원본 수집통 (Static)
    const awsBottom = new CANNON.Body({
      type: CANNON.Body.STATIC,
      shape: new CANNON.Box(new CANNON.Vec3(0.95, 0.08, 0.75)),
      position: new CANNON.Vec3(-2.35, -1.25, 0),
    });
    const awsLeft = new CANNON.Body({
      type: CANNON.Body.STATIC,
      shape: new CANNON.Box(new CANNON.Vec3(0.08, 0.45, 0.75)),
      position: new CANNON.Vec3(-3.3, -0.8, 0),
    });
    const awsRight = new CANNON.Body({
      type: CANNON.Body.STATIC,
      shape: new CANNON.Box(new CANNON.Vec3(0.08, 0.45, 0.75)),
      position: new CANNON.Vec3(-1.4, -0.8, 0),
    });
    world.addBody(awsBottom);
    world.addBody(awsLeft);
    world.addBody(awsRight);

    // 7. 우측 목적지 수집통 (Static)
    const gcpBottom = new CANNON.Body({
      type: CANNON.Body.STATIC,
      shape: new CANNON.Box(new CANNON.Vec3(0.95, 0.08, 0.75)),
      position: new CANNON.Vec3(2.35, -1.25, 0),
    });
    const gcpLeft = new CANNON.Body({
      type: CANNON.Body.STATIC,
      shape: new CANNON.Box(new CANNON.Vec3(0.08, 0.45, 0.75)),
      position: new CANNON.Vec3(1.4, -0.8, 0),
    });
    const gcpRight = new CANNON.Body({
      type: CANNON.Body.STATIC,
      shape: new CANNON.Box(new CANNON.Vec3(0.08, 0.45, 0.75)),
      position: new CANNON.Vec3(3.3, -0.8, 0),
    });
    world.addBody(gcpBottom);
    world.addBody(gcpLeft);
    world.addBody(gcpRight);

    // 8. Dynamic 트래픽 구슬 풀 생성
    const sphereShape = new CANNON.Sphere(SPHERE_RADIUS);
    const pool: SphereItem[] = [];
    for (let i = 0; i < POOL_SIZE; i++) {
      const b = new CANNON.Body({
        mass: 0.6,
        shape: sphereShape,
        position: new CANNON.Vec3(0, -100, 0),
      });
      world.addBody(b);
      pool.push({
        body: b,
        mesh: null,
        active: false,
        bornAt: 0,
      });
    }
    spheresRef.current = pool;

    return () => {
      spheresRef.current.forEach((item) => world.removeBody(item.body));
      worldRef.current = null;
    };
  }, []);

  const isHolding = phase === "hold" || phase === "switch";
  const isTargetRoute = phase === "switch" || phase === "target";
  const isKnownRoute = phase === "source" || isHolding || phase === "target";

  // 모르는 상태와 실패 기록은 밸브를 수평(중립)으로 둡니다.
  const targetValveAngle = !isKnownRoute ? 0 : isTargetRoute ? -0.42 : 0.42;
  const currentValveAngleRef = useRef(targetValveAngle);
  const currentHoldXRef = useRef(isHolding ? 0 : -1.25);

  useFrame(({ clock }, delta) => {
    const now = clock.getElapsedTime();
    const world = worldRef.current;
    if (!world) return;

    // 1. 보류 댐 게이트 슬라이딩 제어 (HOLD / VERIFY / SWITCH 동안 닫힘)
    const targetHoldX = isHolding ? 0.0 : -1.25;
    currentHoldXRef.current = THREE.MathUtils.lerp(
      currentHoldXRef.current,
      targetHoldX,
      Math.min(1, delta * 9.0)
    );
    if (holdGateBodyRef.current) {
      holdGateBodyRef.current.position.x = currentHoldXRef.current;
    }
    if (holdGateMeshRef.current) {
      holdGateMeshRef.current.position.x = currentHoldXRef.current;
    }

    // 2. 라우팅 전환 밸브 회전 제어 (원본 +24도 왼쪽 낙하 ↔ 목적지 -24도 오른쪽 낙하, 모름은 수평)
    currentValveAngleRef.current = THREE.MathUtils.lerp(
      currentValveAngleRef.current,
      targetValveAngle,
      Math.min(1, delta * 6.0)
    );
    if (valveBodyRef.current) {
      valveBodyRef.current.quaternion.setFromAxisAngle(
        new CANNON.Vec3(0, 0, 1),
        currentValveAngleRef.current
      );
    }
    if (valveMeshRef.current) {
      valveMeshRef.current.rotation.z = currentValveAngleRef.current;
    }

    // 3. 상단 요청 스폰: 실측 비율만큼만 만듭니다. 상태를 모르면 경로를 알 수 없으므로 만들지 않습니다.
    const rate = isKnownRoute && ballsPerSec !== null && ballsPerSec > 0 ? ballsPerSec : 0;
    if (rate > 0) {
      // 탭이 숨겨졌다 돌아온 첫 프레임의 긴 delta 로 구슬이 한꺼번에 쏟아지지 않게 자릅니다.
      spawnTimerRef.current += Math.min(delta, 0.1);
      const interval = 1.0 / rate;
      while (spawnTimerRef.current >= interval) {
        spawnTimerRef.current -= interval;
        const candidate = spheresRef.current.find((s) => !s.active);
        if (!candidate) {
          // 풀이 차면 밀린 몫을 쌓아 두지 않습니다 (나중에 없던 급증처럼 보이지 않게).
          spawnTimerRef.current = 0;
          break;
        }
        const [ox, oz] = SPAWN_OFFSETS[spawnIndexRef.current % SPAWN_OFFSETS.length];
        spawnIndexRef.current += 1;
        candidate.active = true;
        candidate.bornAt = now;
        candidate.body.position.set(ox, 2.85, oz);
        candidate.body.velocity.set(0, -0.4, 0);
        candidate.body.angularVelocity.set(0, 0, 0);
      }
    } else {
      spawnTimerRef.current = 0;
    }

    world.step(1 / 60, Math.min(delta, 0.1));

    spheresRef.current.forEach((item) => {
      if (!item.active) {
        if (item.mesh) item.mesh.visible = false;
        return;
      }
      if (item.mesh) {
        item.mesh.visible = true;
        item.mesh.position.set(
          item.body.position.x,
          item.body.position.y,
          item.body.position.z
        );
        item.mesh.quaternion.set(
          item.body.quaternion.x,
          item.body.quaternion.y,
          item.body.quaternion.z,
          item.body.quaternion.w
        );
      }

      const age = now - item.bornAt;
      // 보류 중일 때는 버퍼 내부 구슬이 사라지지 않도록 유지
      const maxAge = isHolding && item.body.position.y > 1.6 ? 15 : 4.8;
      if (age > maxAge || item.body.position.y < -2.1) {
        item.active = false;
        item.body.position.set(0, -100, 0);
        item.body.velocity.set(0, 0, 0);
      }
    });

  });

  return (
    <group>
      {/* 1. 상단 Cloudflare Worker 요청 보류(Hold) 슈트 */}
      <group position={[0, 2.35, 0]}>
        <mesh position={[-0.55, 0, 0]} castShadow>
          <boxGeometry args={[0.12, 1.2, 1.2]} />
          <meshStandardMaterial color="#ffffff" roughness={0.6} />
          <Edges color="#1c1917" threshold={15} />
        </mesh>
        <mesh position={[0.55, 0, 0]} castShadow>
          <boxGeometry args={[0.12, 1.2, 1.2]} />
          <meshStandardMaterial color="#ffffff" roughness={0.6} />
          <Edges color="#1c1917" threshold={15} />
        </mesh>

        {/* 슬라이딩 보류 게이트 (Hold Dam) */}
        <mesh ref={holdGateMeshRef} position={[-1.25, -0.63, 0]} castShadow>
          <boxGeometry args={[1.05, 0.12, 1.18]} />
          <meshStandardMaterial color={isHolding ? "#b45309" : "#d6d3cd"} />
          <Edges color="#1c1917" threshold={15} />
        </mesh>

        <Html position={[1.65, 0.15, 0]} center distanceFactor={9} zIndexRange={[10, 0]}>
          <div className="whitespace-nowrap rounded border border-ink/30 bg-white px-2 py-0.5 font-mono text-[10px] font-medium text-ink shadow-[1px_1px_0_0_var(--color-ink)]">
            트래픽 게이트 (Worker) · {isHolding ? "요청 보류(HOLD)" : isKnownRoute ? "통과" : "상태 미제공"}
          </div>
        </Html>
      </group>

      {/* 2. 중앙 라우팅 전환 밸브 (Kinematic Rotary Ramp) */}
      <group position={[0, 0.65, 0]}>
        <mesh ref={valveMeshRef} castShadow receiveShadow>
          <boxGeometry args={[2.5, 0.15, 1.35]} />
          <meshStandardMaterial
            color={!isKnownRoute ? (phase === "failed" ? "#be123c" : "#a8a29e") : isTargetRoute ? "#047857" : "#3730a3"}
            roughness={0.5}
          />
          <Edges color="#1c1917" threshold={15} />
        </mesh>
        <mesh rotation={[Math.PI / 2, 0, 0]}>
          <cylinderGeometry args={[0.14, 0.14, 1.5, 20]} />
          <meshStandardMaterial color="#1c1917" />
        </mesh>
      </group>

      {/* 3. 좌측 원본 수집통 (Static) */}
      <group position={[-2.35, -0.95, 0]}>
        <mesh position={[0, -0.3, 0]} receiveShadow>
          <boxGeometry args={[1.9, 0.12, 1.35]} />
          <meshStandardMaterial color="#ffffff" />
          <Edges color="#1c1917" threshold={15} />
        </mesh>
        <mesh position={[-0.92, 0.12, 0]} castShadow>
          <boxGeometry args={[0.12, 0.75, 1.35]} />
          <meshStandardMaterial color="#ffffff" />
          <Edges color="#1c1917" threshold={15} />
        </mesh>
        <mesh position={[0.92, 0.12, 0]} castShadow>
          <boxGeometry args={[0.12, 0.75, 1.35]} />
          <meshStandardMaterial color="#ffffff" />
          <Edges color="#1c1917" threshold={15} />
        </mesh>
        <Html position={[0, -0.68, 0]} center distanceFactor={9} zIndexRange={[10, 0]}>
          <div className="whitespace-nowrap rounded border border-ink bg-white px-2 py-0.5 font-mono text-[10px] font-semibold text-ink shadow-[2px_2px_0_0_var(--color-ink)]">
            원본 {source.cloudName} ({source.region ?? "미제공"})
          </div>
        </Html>
      </group>

      {/* 4. 우측 목적지 수집통 (Static) */}
      <group position={[2.35, -0.95, 0]}>
        <mesh position={[0, -0.3, 0]} receiveShadow>
          <boxGeometry args={[1.9, 0.12, 1.35]} />
          <meshStandardMaterial color={phase === "target" ? "#ecfdf5" : "#ffffff"} />
          <Edges color="#1c1917" threshold={15} />
        </mesh>
        <mesh position={[-0.92, 0.12, 0]} castShadow>
          <boxGeometry args={[0.12, 0.75, 1.35]} />
          <meshStandardMaterial color="#ffffff" />
          <Edges color="#1c1917" threshold={15} />
        </mesh>
        <mesh position={[0.92, 0.12, 0]} castShadow>
          <boxGeometry args={[0.12, 0.75, 1.35]} />
          <meshStandardMaterial color="#ffffff" />
          <Edges color="#1c1917" threshold={15} />
        </mesh>
        <Html position={[0, -0.68, 0]} center distanceFactor={9} zIndexRange={[10, 0]}>
          <div className="whitespace-nowrap rounded border border-ink bg-white px-2 py-0.5 font-mono text-[10px] font-semibold text-emerald-800 shadow-[2px_2px_0_0_var(--color-ink)]">
            목적지 {target.providerSummary}
          </div>
        </Html>
      </group>

      {/* 5. 트래픽 구슬 (Dynamic Spheres): 실측 요청 비율을 눈에 보이게 옮긴 것이며 구슬 수가 요청 수는 아닙니다 */}
      {Array.from({ length: POOL_SIZE }).map((_, idx) => (
        <mesh
          key={idx}
          ref={(el) => {
            if (spheresRef.current[idx]) {
              spheresRef.current[idx].mesh = el;
            }
          }}
          visible={false}
          castShadow
        >
          <sphereGeometry args={[SPHERE_RADIUS, 14, 14]} />
          <meshStandardMaterial
            color={isTargetRoute ? "#047857" : "#3730a3"}
            roughness={0.35}
          />
          <Edges color="#1c1917" threshold={25} />
        </mesh>
      ))}

      {/* 6. 바닥 그리드 & 그림자 */}
      <group position={[0, -1.68, 0]}>
        <gridHelper args={[14, 14, 0xd6d3cd, 0xe7e5e0]} />
        <ContactShadows
          position={[0, 0.01, 0]}
          opacity={0.25}
          scale={12}
          blur={1.8}
          far={2.5}
          color="#1c1917"
        />
      </group>
    </group>
  );
}
