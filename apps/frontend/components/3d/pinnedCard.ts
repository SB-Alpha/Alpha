import * as THREE from "three";

// 3D 뷰포트(높이 240px)는 작아서, 노드 위에 띄운 상세 카드가 장면을 통째로 가립니다.
// 상세 카드는 노드 위 대신 캔버스 모서리에 고정하고, 노드가 있는 쪽의 반대편에 둡니다.
// drei <Html> 의 calculatePosition 으로 씁니다 (center 없이). 반환값은 카드 왼쪽 위 꼭짓점의 화면 좌표입니다.

/** 상세 카드 너비(px). 카드의 style.width 와 같아야 좌우 위치가 맞습니다. */
export const DETAIL_CARD_WIDTH = 184;
/** 캔버스 가장자리와의 간격(px). */
const EDGE_GAP = 8;
/** 하단 요약 바(복사율·복제 지연)를 가리지 않도록 아래쪽 카드를 띄우는 높이(px). 좁은 화면에서는 바가 여러 줄로 접힙니다. */
const BOTTOM_BAR_GAP = 44;
const BOTTOM_BAR_GAP_NARROW = 78;
const NARROW_CANVAS_WIDTH = 640;
/** 카드 최대 높이(px). 캔버스 높이 240px 에서 위·아래 간격을 뺀 값입니다. */
export const DETAIL_CARD_MAX_HEIGHT = 240 - EDGE_GAP * 2;

type Size = { width: number; height: number };
type Placement = "top" | "bottom";

const projected = new THREE.Vector3();

/**
 * 노드가 화면 왼쪽 절반에 있으면 오른쪽 모서리에, 오른쪽 절반에 있으면 왼쪽 모서리에 카드를 둡니다.
 * "bottom" 은 앵커를 아래쪽에 두며, 카드 쪽에서 `-translate-y-full` 로 자기 높이만큼 올려야 합니다.
 */
export function pinAwayFromNode(placement: Placement = "top") {
  return (el: THREE.Object3D, camera: THREE.Camera, size: Size): [number, number] => {
    projected.setFromMatrixPosition(el.matrixWorld).project(camera);
    const nodeX = ((projected.x + 1) / 2) * size.width;
    const x = nodeX < size.width / 2 ? Math.max(EDGE_GAP, size.width - DETAIL_CARD_WIDTH - EDGE_GAP) : EDGE_GAP;
    const bottomGap = size.width < NARROW_CANVAS_WIDTH ? BOTTOM_BAR_GAP_NARROW : BOTTOM_BAR_GAP;
    const y = placement === "top" ? EDGE_GAP : size.height - bottomGap;
    return [x, y];
  };
}

/** 고정 상세 카드의 공통 겉모양 (너비·높이 제한·스크롤). */
export const DETAIL_CARD_CLASS =
  "pointer-events-auto overflow-y-auto rounded-md border border-ink bg-white/97 p-1.5 text-left text-ink shadow-[2px_2px_0_0_var(--color-ink)]";

export const DETAIL_CARD_STYLE = { width: DETAIL_CARD_WIDTH, maxHeight: DETAIL_CARD_MAX_HEIGHT } as const;
