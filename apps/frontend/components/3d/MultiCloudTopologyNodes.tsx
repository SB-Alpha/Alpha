"use client";

import { Edges, Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import * as THREE from "three";

interface Props {
  showFullTopology: boolean;
}

export function MultiCloudTopologyNodes({ showFullTopology }: Props) {
  const edgeHexRef = useRef<THREE.Mesh | null>(null);

  useFrame((_, delta) => {
    if (edgeHexRef.current) {
      edgeHexRef.current.rotation.y += delta * 0.5;
    }
  });

  if (!showFullTopology) return null;

  return (
    <group>
      {/* ==================== 1. AZURE NODE (Korea Central) ==================== */}
      <group position={[3.1, 0, 1.8]}>
        <mesh position={[0, -0.35, 0]} receiveShadow>
          <boxGeometry args={[1.5, 0.12, 1.2]} />
          <meshStandardMaterial color="#e0f2fe" roughness={0.7} />
          <Edges color="#0369a1" threshold={15} />
        </mesh>
        {/* Azure Container Apps */}
        <mesh position={[-0.32, 0.22, 0]} castShadow receiveShadow>
          <boxGeometry args={[0.55, 0.95, 0.65]} />
          <meshStandardMaterial color="#0284c7" roughness={0.5} />
          <Edges color="#0c4a6e" threshold={15} />
        </mesh>
        {/* Azure PostgreSQL Flexible */}
        <mesh position={[0.34, 0.18, 0]} castShadow receiveShadow>
          <cylinderGeometry args={[0.28, 0.28, 0.85, 20]} />
          <meshStandardMaterial color="#0369a1" roughness={0.5} />
          <Edges color="#0c4a6e" threshold={15} />
        </mesh>
        <Html position={[0, -0.65, 0]} center zIndexRange={[20, 0]}>
          <div className="whitespace-nowrap rounded border border-sky-800 bg-sky-50 px-2 py-0.5 font-mono text-[10px] font-semibold text-sky-900 shadow-[1px_1px_0_0_#0369a1]">
            Azure (Korea Central) · Container Apps
          </div>
        </Html>
      </group>

      {/* ==================== 2. ON-PREMISE NODE (IDC / OCI DRG) ==================== */}
      <group position={[-3.1, 0, 1.8]}>
        <mesh position={[0, -0.35, 0]} receiveShadow>
          <boxGeometry args={[1.5, 0.12, 1.2]} />
          <meshStandardMaterial color="#e7e5e4" roughness={0.8} />
          <Edges color="#44403c" threshold={15} />
        </mesh>
        {/* 온프레미스 서버 랙 */}
        <mesh position={[-0.32, 0.28, 0]} castShadow receiveShadow>
          <boxGeometry args={[0.62, 1.1, 0.68]} />
          <meshStandardMaterial color="#57534e" roughness={0.8} />
          <Edges color="#1c1917" threshold={15} />
        </mesh>
        {/* Debezium CDC Connector */}
        <mesh position={[0.34, 0.16, 0]} castShadow receiveShadow>
          <cylinderGeometry args={[0.26, 0.26, 0.8, 16]} />
          <meshStandardMaterial color="#78716c" roughness={0.6} />
          <Edges color="#1c1917" threshold={15} />
        </mesh>
        <Html position={[0, -0.65, 0]} center zIndexRange={[20, 0]}>
          <div className="whitespace-nowrap rounded border border-stone-800 bg-stone-100 px-2 py-0.5 font-mono text-[10px] font-semibold text-stone-900 shadow-[1px_1px_0_0_#292524]">
            On-Premise (IPsec DRG + CDC Node)
          </div>
        </Html>
      </group>

      {/* ==================== 3. CLOUDFLARE EDGE ROUTER (중앙 관제) ==================== */}
      <group position={[0, 1.2, 0.9]}>
        <mesh ref={edgeHexRef} castShadow>
          <cylinderGeometry args={[0.32, 0.32, 0.18, 6]} />
          <meshStandardMaterial color="#ea580c" emissive="#c2410c" emissiveIntensity={0.5} roughness={0.3} />
          <Edges color="#7c2d12" threshold={15} />
        </mesh>
        <Html position={[0, 0.45, 0]} center zIndexRange={[30, 10]}>
          <div className="whitespace-nowrap rounded border border-orange-800 bg-orange-50 px-2 py-0.5 font-mono text-[10px] font-bold text-orange-950 shadow-[1px_1px_0_0_#9a3412]">
            ⚡ Cloudflare Global Edge (41.5ms Anycast)
          </div>
        </Html>
      </group>
    </group>
  );
}
