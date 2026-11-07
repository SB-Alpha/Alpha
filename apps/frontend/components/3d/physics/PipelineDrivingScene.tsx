"use client";

import { ContactShadows, Edges, Html } from "@react-three/drei";
import { useFrame, useThree } from "@react-three/fiber";
import * as CANNON from "cannon-es";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { extractStageReports, type StageReportCardData } from "@/lib/topology3d";
import type { RunStatus } from "@/lib/workflows";

export type CameraMode = "chase" | "cockpit" | "topdown";

export interface DrivingControlState {
  forward: boolean;
  backward: boolean;
  left: boolean;
  right: boolean;
  brake: boolean;
  resetSignal: number;
}

interface Props {
  run: RunStatus;
  controls: DrivingControlState;
  cameraMode?: CameraMode;
  onToggleCameraMode?: (mode: CameraMode) => void;
  onCheckpointChange?: (checkpoint: StageReportCardData, speedKmh: number, gear?: "P" | "D" | "R") => void;
}

const STAGE_Z_POSITIONS = [-4, -13, -22, -31, -40, -49];

export function PipelineDrivingScene({
  run,
  controls,
  cameraMode = "chase",
  onToggleCameraMode,
  onCheckpointChange,
}: Props) {
  const { camera } = useThree();
  const stageReports = useMemo(() => extractStageReports(run), [run]);
  const stageStateSignature = useMemo(
    () => run.stages.map((s) => `${s.stage}:${s.state}`).join("|"),
    [run.stages],
  );

  const worldRef = useRef<CANNON.World | null>(null);
  const vehicleRef = useRef<CANNON.RaycastVehicle | null>(null);
  const chassisMeshRef = useRef<THREE.Group | null>(null);
  const wheelMeshesRef = useRef<(THREE.Group | null)[]>([null, null, null, null]);

  // 통과 가능한 스테이지의 동적 축하 블록들 (충돌 시 날아가는 물리 박스)
  interface DynamicBlock {
    body: CANNON.Body;
    mesh: THREE.Mesh | null;
  }
  const dynamicBlocksRef = useRef<DynamicBlock[]>([]);
  const lastStageIdxRef = useRef<number>(-1);
  const lastSpeedRef = useRef<number>(0);

  // 키보드 입력 리스너 (WASD / 화살표)
  const keyStateRef = useRef({
    forward: false,
    backward: false,
    left: false,
    right: false,
    brake: false,
  });

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement ||
        e.target instanceof HTMLSelectElement ||
        e.target instanceof HTMLButtonElement
      ) {
        return;
      }
      switch (e.key.toLowerCase()) {
        case "w":
        case "arrowup":
          keyStateRef.current.forward = true;
          break;
        case "s":
        case "arrowdown":
          keyStateRef.current.backward = true;
          break;
        case "a":
        case "arrowleft":
          keyStateRef.current.left = true;
          break;
        case "d":
        case "arrowright":
          keyStateRef.current.right = true;
          break;
        case " ":
          keyStateRef.current.brake = true;
          break;
        case "c": {
          const nextMode: CameraMode =
            cameraMode === "chase"
              ? "cockpit"
              : cameraMode === "cockpit"
                ? "topdown"
                : "chase";
          onToggleCameraMode?.(nextMode);
          break;
        }
      }
    };

    const onKeyUp = (e: KeyboardEvent) => {
      switch (e.key.toLowerCase()) {
        case "w":
        case "arrowup":
          keyStateRef.current.forward = false;
          break;
        case "s":
        case "arrowdown":
          keyStateRef.current.backward = false;
          break;
        case "a":
        case "arrowleft":
          keyStateRef.current.left = false;
          break;
        case "d":
        case "arrowright":
          keyStateRef.current.right = false;
          break;
        case " ":
          keyStateRef.current.brake = false;
          break;
      }
    };

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
    };
  }, [cameraMode, onToggleCameraMode]);

  // Cannon-es RaycastVehicle 및 트랙 물리 월드 초기화 (Project 3 Ch.2 핵심)
  useEffect(() => {
    const world = new CANNON.World();
    world.gravity.set(0, -9.82, 0);
    world.broadphase = new CANNON.SAPBroadphase(world);
    world.defaultContactMaterial.friction = 0.3;
    worldRef.current = world;

    // 1. 지면 바닥 (Static)
    const groundBody = new CANNON.Body({
      type: CANNON.Body.STATIC,
      shape: new CANNON.Box(new CANNON.Vec3(8, 0.2, 40)),
      position: new CANNON.Vec3(0, -0.2, -25),
    });
    world.addBody(groundBody);

    // 2. 트랙 좌/우 가드레일 투명 벽 (Invisible Walls)
    const leftRail = new CANNON.Body({
      type: CANNON.Body.STATIC,
      shape: new CANNON.Box(new CANNON.Vec3(0.2, 2, 40)),
      position: new CANNON.Vec3(-3.6, 1, -25),
    });
    const rightRail = new CANNON.Body({
      type: CANNON.Body.STATIC,
      shape: new CANNON.Box(new CANNON.Vec3(0.2, 2, 40)),
      position: new CANNON.Vec3(3.6, 1, -25),
    });
    const backWall = new CANNON.Body({
      type: CANNON.Body.STATIC,
      shape: new CANNON.Box(new CANNON.Vec3(6, 2, 0.2)),
      position: new CANNON.Vec3(0, 1, 6),
    });
    const finishWall = new CANNON.Body({
      type: CANNON.Body.STATIC,
      shape: new CANNON.Box(new CANNON.Vec3(6, 2, 0.2)),
      position: new CANNON.Vec3(0, 1, -56),
    });
    world.addBody(leftRail);
    world.addBody(rightRail);
    world.addBody(backWall);
    world.addBody(finishWall);

    // 3. 차량 섀시 (CompoundBody / Box)
    const chassisBody = new CANNON.Body({
      mass: 120,
      position: new CANNON.Vec3(0, 0.8, 2),
      angularDamping: 0.4,
      linearDamping: 0.15,
    });
    chassisBody.addShape(new CANNON.Box(new CANNON.Vec3(0.65, 0.25, 1.1)));

    const vehicle = new CANNON.RaycastVehicle({
      chassisBody,
      indexRightAxis: 0,
      indexUpAxis: 1,
      indexForwardAxis: 2,
    });

    const wheelOptions = {
      radius: 0.28,
      directionLocal: new CANNON.Vec3(0, -1, 0),
      suspensionStiffness: 35,
      suspensionRestLength: 0.25,
      frictionSlip: 2.5,
      dampingRelaxation: 2.3,
      dampingCompression: 4.4,
      maxSuspensionForce: 100000,
      rollInfluence: 0.01,
      axleLocal: new CANNON.Vec3(-1, 0, 0),
      chassisConnectionPointLocal: new CANNON.Vec3(0, 0, 0),
      maxSuspensionTravel: 0.2,
      customSlidingRotationalSpeed: -30,
      useCustomSlidingRotationalSpeed: true,
    };

    // 4륜 장착 (전륜 0,1 / 후륜 2,3)
    const wheelPositions = [
      new CANNON.Vec3(-0.68, 0.0, -0.72),
      new CANNON.Vec3(0.68, 0.0, -0.72),
      new CANNON.Vec3(-0.68, 0.0, 0.72),
      new CANNON.Vec3(0.68, 0.0, 0.72),
    ];
    wheelPositions.forEach((pos) => {
      vehicle.addWheel({
        ...wheelOptions,
        chassisConnectionPointLocal: pos,
      });
    });

    vehicle.addToWorld(world);
    vehicleRef.current = vehicle;

    // 4. 각 스테이지 상태에 따른 물리 블록 배치
    // 성공(succeeded)한 단계에는 차량이 치고 지나갈 수 있는 가벼운 Dynamic 블록을 놓고,
    // 실패(failed)나 미승인(waiting_approval/pending) 단계에는 실제 차단 바리케이드(Static)를 세웁니다.
    const dynBlocks: DynamicBlock[] = [];
    const currentStageReports = extractStageReports(run);
    currentStageReports.forEach((sr, idx) => {
      const z = STAGE_Z_POSITIONS[idx] ?? -4 - idx * 9;
      if (sr.state === "succeeded") {
        for (const x of [-0.45, 0.45]) {
          const b = new CANNON.Body({
            mass: 2.5,
            shape: new CANNON.Box(new CANNON.Vec3(0.22, 0.22, 0.22)),
            position: new CANNON.Vec3(x, 0.25, z),
          });
          world.addBody(b);
          dynBlocks.push({ body: b, mesh: null });
        }
      } else {
        // 미완료/실패 단계는 물리 차단벽 생성
        const barrier = new CANNON.Body({
          type: CANNON.Body.STATIC,
          shape: new CANNON.Box(new CANNON.Vec3(1.8, 0.6, 0.25)),
          position: new CANNON.Vec3(0, 0.6, z),
        });
        world.addBody(barrier);
      }
    });
    dynamicBlocksRef.current = dynBlocks;

    return () => {
      vehicle.removeFromWorld(world);
      worldRef.current = null;
    };
    // 2초마다 일어나는 run 폴링 시 물리 월드가 리셋되지 않도록 실제 스테이지 상태 시그니처만 의존합니다.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stageStateSignature]);

  // 위치 리셋 시그널 처리
  useEffect(() => {
    if (controls.resetSignal === 0) return;
    const v = vehicleRef.current;
    if (!v) return;
    v.chassisBody.position.set(0, 0.9, 2);
    v.chassisBody.velocity.set(0, 0, 0);
    v.chassisBody.angularVelocity.set(0, 0, 0);
    v.chassisBody.quaternion.set(0, 0, 0, 1);
  }, [controls.resetSignal]);

  const camTargetPos = useMemo(() => new THREE.Vector3(), []);
  const camLookAtPos = useMemo(() => new THREE.Vector3(), []);

  useFrame((_, delta) => {
    const world = worldRef.current;
    const vehicle = vehicleRef.current;
    if (!world || !vehicle) return;

    const fwd = keyStateRef.current.forward || controls.forward;
    const bwd = keyStateRef.current.backward || controls.backward;
    const left = keyStateRef.current.left || controls.left;
    const right = keyStateRef.current.right || controls.right;
    const brk = keyStateRef.current.brake || controls.brake;

    const maxForce = 320;
    const maxSteer = 0.42;

    // -Z 방향이 전진 트랙
    let engineForce = 0;
    if (fwd) engineForce = maxForce;
    else if (bwd) engineForce = -maxForce * 0.75;

    let steering = 0;
    if (left) steering = maxSteer;
    else if (right) steering = -maxSteer;

    vehicle.applyEngineForce(engineForce, 2);
    vehicle.applyEngineForce(engineForce, 3);
    vehicle.setSteeringValue(steering, 0);
    vehicle.setSteeringValue(steering, 1);

    const brakeForce = brk ? 18 : !fwd && !bwd ? 1.5 : 0;
    for (let i = 0; i < 4; i++) {
      vehicle.setBrake(brakeForce, i);
    }

    world.step(1 / 60, Math.min(delta, 0.1));

    // 섀시 동기화
    const chassisPos = vehicle.chassisBody.position;
    const chassisQuat = vehicle.chassisBody.quaternion;
    if (chassisMeshRef.current) {
      chassisMeshRef.current.position.set(chassisPos.x, chassisPos.y, chassisPos.z);
      chassisMeshRef.current.quaternion.set(
        chassisQuat.x,
        chassisQuat.y,
        chassisQuat.z,
        chassisQuat.w
      );
    }

    // 바퀴 4개 동기화
    for (let i = 0; i < vehicle.wheelInfos.length; i++) {
      vehicle.updateWheelTransform(i);
      const t = vehicle.wheelInfos[i].worldTransform;
      const wMesh = wheelMeshesRef.current[i];
      if (wMesh) {
        wMesh.position.set(t.position.x, t.position.y, t.position.z);
        wMesh.quaternion.set(
          t.quaternion.x,
          t.quaternion.y,
          t.quaternion.z,
          t.quaternion.w
        );
      }
    }

    // 동적 블록 동기화
    dynamicBlocksRef.current.forEach((item) => {
      if (item.mesh) {
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
    });

    // Camera Handling based on CameraMode
    if (cameraMode === "cockpit") {
      // 1인칭 운전석 콕핏 뷰
      camTargetPos.set(chassisPos.x, chassisPos.y + 0.58, chassisPos.z - 0.25);
      camLookAtPos.set(chassisPos.x, chassisPos.y + 0.54, chassisPos.z - 8.0);
      camera.position.lerp(camTargetPos, Math.min(1, delta * 12.0));
      camera.lookAt(camLookAtPos);
    } else if (cameraMode === "topdown") {
      // 상공 버드아이 탑뷰 조감도
      camTargetPos.set(0, 16.5, chassisPos.z - 2.5);
      camLookAtPos.set(0, 0, chassisPos.z - 5.5);
      camera.position.lerp(camTargetPos, Math.min(1, delta * 6.0));
      camera.lookAt(camLookAtPos);
    } else {
      // 3인칭 추격 카메라 (기본)
      camTargetPos.set(chassisPos.x * 0.6, chassisPos.y + 2.4, chassisPos.z + 5.2);
      camLookAtPos.set(chassisPos.x * 0.4, chassisPos.y + 0.3, chassisPos.z - 4.0);
      camera.position.lerp(camTargetPos, Math.min(1, delta * 5.0));
      camera.lookAt(camLookAtPos);
    }

    // 현재 가장 가까운 체크포인트 계산 및 상위 HUD 전달
    let closestIdx = 0;
    let minDist = Infinity;
    STAGE_Z_POSITIONS.forEach((sz, idx) => {
      const d = Math.abs(chassisPos.z - sz);
      if (d < minDist) {
        minDist = d;
        closestIdx = idx;
      }
    });

    const speedMs = vehicle.chassisBody.velocity.length();
    const speedKmh = Math.round(speedMs * 3.6);
    const forwardVelZ = -vehicle.chassisBody.velocity.z;
    const gear: "P" | "D" | "R" =
      Math.abs(forwardVelZ) < 0.15 ? "P" : forwardVelZ > 0.15 ? "D" : "R";
    lastSpeedRef.current = speedKmh;

    if (onCheckpointChange && stageReports[closestIdx]) {
      if (closestIdx !== lastStageIdxRef.current || speedKmh > 0) {
        lastStageIdxRef.current = closestIdx;
        onCheckpointChange(stageReports[closestIdx], speedKmh, gear);
      }
    }
  });

  // 처음에는 첫 체크포인트를 알립니다. 폴링으로 보고서가 바뀌면 지금 위치의 카드와 속도를 유지합니다.
  useEffect(() => {
    const report = stageReports[Math.max(lastStageIdxRef.current, 0)];
    if (report && onCheckpointChange) {
      onCheckpointChange(report, lastSpeedRef.current);
    }
  }, [stageReports, onCheckpointChange]);

  return (
    <>
      <color attach="background" args={["#f6f4ef"]} />

      <ambientLight intensity={1.15} />
      <directionalLight position={[8, 14, 10]} intensity={1.1} castShadow />
      <directionalLight position={[-8, 8, -20]} intensity={0.5} />

      {/* 메인 하이웨이 도로 & 그리드 */}
      <group position={[0, 0, -25]}>
        <mesh position={[0, -0.02, 0]} receiveShadow>
          <boxGeometry args={[6.4, 0.04, 66]} />
          <meshStandardMaterial color="#e7e5e0" roughness={0.85} />
          <Edges color="#1c1917" threshold={15} />
        </mesh>
        {/* 중앙 차선 */}
        <mesh position={[0, 0.01, 0]}>
          <boxGeometry args={[0.12, 0.01, 64]} />
          <meshBasicMaterial color="#1c1917" />
        </mesh>
        <ContactShadows
          position={[0, 0.02, 0]}
          opacity={0.22}
          scale={50}
          blur={1.8}
          far={2.5}
          color="#1c1917"
        />
      </group>

      {/* RaycastVehicle 차량 섀시 */}
      <group ref={chassisMeshRef}>
        {/* 하부 바디 */}
        <mesh castShadow receiveShadow>
          <boxGeometry args={[1.3, 0.36, 2.15]} />
          <meshStandardMaterial color="#1c1917" roughness={0.4} />
          <Edges color="#f6f4ef" threshold={15} />
        </mesh>
        {/* 상부 캐빈 */}
        <mesh position={[0, 0.28, 0.1]} castShadow receiveShadow>
          <boxGeometry args={[1.05, 0.28, 1.1]} />
          <meshStandardMaterial color="#ffffff" roughness={0.4} />
          <Edges color="#1c1917" threshold={15} />
        </mesh>
        {/* 전면/후면 상태 라이트 바 */}
        <mesh position={[0, 0.04, -1.08]}>
          <boxGeometry args={[0.9, 0.08, 0.02]} />
          <meshBasicMaterial color="#3730a3" />
        </mesh>
        <mesh position={[0, 0.04, 1.08]}>
          <boxGeometry args={[0.9, 0.08, 0.02]} />
          <meshBasicMaterial color="#047857" />
        </mesh>
      </group>

      {/* 바퀴 4개 */}
      {[0, 1, 2, 3].map((i) => (
        <group
          key={i}
          ref={(el) => {
            wheelMeshesRef.current[i] = el;
          }}
        >
          <mesh rotation={[0, 0, Math.PI / 2]} castShadow>
            <cylinderGeometry args={[0.28, 0.28, 0.22, 16]} />
            <meshStandardMaterial color="#1c1917" roughness={0.7} />
            <Edges color="#d6d3cd" threshold={20} />
          </mesh>
        </group>
      ))}

      {/* 6개 실제 마이그레이션 체크포인트 게이트 & 3D 표지판 */}
      {stageReports.map((sr, idx) => {
        const z = STAGE_Z_POSITIONS[idx] ?? -4 - idx * 9;
        const isPassed = sr.state === "succeeded";
        const isFailed = sr.state === "failed";
        const accentColor = isPassed ? "#047857" : isFailed ? "#be123c" : "#b45309";

        return (
          <group key={sr.stage} position={[0, 0, z]}>
            {/* 좌/우 게이트 기둥 */}
            <mesh position={[-2.6, 1.1, 0]} castShadow receiveShadow>
              <boxGeometry args={[0.3, 2.2, 0.3]} />
              <meshStandardMaterial color="#ffffff" />
              <Edges color="#1c1917" threshold={15} />
            </mesh>
            <mesh position={[2.6, 1.1, 0]} castShadow receiveShadow>
              <boxGeometry args={[0.3, 2.2, 0.3]} />
              <meshStandardMaterial color="#ffffff" />
              <Edges color="#1c1917" threshold={15} />
            </mesh>
            {/* 상단 아치 빔 */}
            <mesh position={[0, 2.15, 0]} castShadow>
              <boxGeometry args={[5.5, 0.32, 0.35]} />
              <meshStandardMaterial color={accentColor} />
              <Edges color="#1c1917" threshold={15} />
            </mesh>

            {/* 미완료/실패 단계는 실제 차단 바리케이드 렌더링 */}
            {!isPassed && (
              <mesh position={[0, 0.5, 0]} castShadow receiveShadow>
                <boxGeometry args={[3.6, 0.9, 0.4]} />
                <meshStandardMaterial color={accentColor} />
                <Edges color="#1c1917" threshold={15} />
              </mesh>
            )}

            {/* 게이트 상단 실측 데이터 간판 */}
            <Html position={[0, 2.65, 0]} center distanceFactor={11} zIndexRange={[20, 0]}>
              <div className="whitespace-nowrap rounded border border-ink bg-white px-2.5 py-1 text-center font-mono text-[10px] text-ink shadow-[2px_2px_0_0_var(--color-ink)]">
                <span className="font-bold">
                  {String(sr.index).padStart(2, "0")} {sr.label}
                </span>{" "}
                ·{" "}
                <span
                  className={
                    isPassed
                      ? "font-semibold text-emerald-700"
                      : isFailed
                        ? "font-semibold text-rose-700"
                        : "font-semibold text-amber-800"
                  }
                >
                  {sr.state.toUpperCase()}
                </span>
                {sr.facts[0] && (
                  <span className="ml-1.5 text-ink/65">
                    ({sr.facts[0].label}: {sr.facts[0].value})
                  </span>
                )}
              </div>
            </Html>
          </group>
        );
      })}

      {/* 통과 단계의 물리 축하 박스들 */}
      {stageReports
        .filter((s) => s.state === "succeeded")
        .flatMap((_, sIdx) => [sIdx * 2, sIdx * 2 + 1])
        .map((blockIdx) => (
          <mesh
            key={blockIdx}
            ref={(el) => {
              if (dynamicBlocksRef.current[blockIdx]) {
                dynamicBlocksRef.current[blockIdx].mesh = el;
              }
            }}
            castShadow
            receiveShadow
          >
            <boxGeometry args={[0.44, 0.44, 0.44]} />
            <meshStandardMaterial color="#ffffff" />
            <Edges color="#047857" threshold={15} />
          </mesh>
        ))}
    </>
  );
}
