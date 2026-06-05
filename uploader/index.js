/**
 * Irys 업로드 서비스
 *
 * POST /upload  { payload, signature, publicKey, algorithm }
 *   → { txId, arweaveUrl }
 *
 * GET  /health  → { ok: true }
 *
 * IRYS_NETWORK=devnet|mainnet  (기본: devnet)
 * AGENT_PRIVATE_KEY=0x...       (Ethereum private key, Irys 수수료 지불용)
 */

import Irys from "@irys/sdk";
import express from "express";

const app = express();
app.use(express.json({ limit: "10mb" }));

const NETWORK = process.env.IRYS_NETWORK || "devnet";
const PRIVATE_KEY = process.env.AGENT_PRIVATE_KEY || "";

const RPC_URLS = {
  devnet:  "https://rpc.ankr.com/eth_sepolia",
  mainnet: "https://rpc.ankr.com/eth",
};

// 네트워크별 조회 게이트웨이.
// - devnet: 업로드는 메인넷 arweave.net 에서 조회되지 않고 devnet.irys.xyz 에서만
//   (삭제 전까지) 임시 조회된다.
// - mainnet: Irys 게이트웨이는 번들이 Arweave 에 정산되기 전에도 optimistic 하게 즉시
//   서빙한다. arweave.net 은 정산이 끝나야 해당 tx 를 서빙하므로 박제 '직후'엔 404 가 난다.
//   따라서 박제 직후에도 바로 열리도록 mainnet 도 gateway.irys.xyz 를 1차 링크로 쓴다.
//   (영구성 증빙용 arweave.net 링크는 UI 에서 보조로 안내)
const GATEWAYS = {
  devnet:  (txId) => `https://devnet.irys.xyz/${txId}`,
  mainnet: (txId) => `https://gateway.irys.xyz/${txId}`,
};
const gatewayUrl = (txId) => (GATEWAYS[NETWORK] || GATEWAYS.devnet)(txId);
const IS_PERMANENT = NETWORK === "mainnet";

async function getIrys() {
  if (!PRIVATE_KEY) {
    throw new Error("AGENT_PRIVATE_KEY 환경변수가 설정되지 않았습니다");
  }
  const irys = new Irys({
    network: NETWORK,
    token: "ethereum",
    key: PRIVATE_KEY,
    config: { providerUrl: RPC_URLS[NETWORK] || RPC_URLS.devnet },
  });
  await irys.ready();
  return irys;
}

app.post("/upload", async (req, res) => {
  try {
    const body = req.body;
    if (!body?.payload) {
      return res.status(400).json({ error: "payload 필드가 필요합니다" });
    }

    const irys = await getIrys();
    const data = JSON.stringify(body);

    const ev = body.payload?.evidence || {};
    const tags = [
      { name: "Content-Type",    value: "application/json" },
      { name: "App-Name",        value: "Heart-Critique" },
      { name: "App-Version",     value: "6.0" },
      { name: "Category",        value: body.payload?.story?.category || "unknown" },
      { name: "Vote-Count",      value: String(body.payload?.votes?.count || 0) },
      { name: "Signed-By",       value: body.publicKey?.slice(0, 20) || "" },
      // Story-Id: 멱등성/중복 박제 탐지용. 동일 App-Name+Story-Id 를 GraphQL 로 조회하면
      // 이미 박제됐는지 확인 가능. (앱 계층의 __pending__ 선점이 1차 방어, 이 태그는 2차)
      { name: "Story-Id",        value: String(body.payload?.story?.id || "") },
      // 검열 증거를 Arweave 인덱싱 태그로도 노출 → GraphQL 로 검색 가능
      { name: "Gap-Score",       value: String(ev.gap_score || "none") },
      { name: "Deleted-Count",   value: String(ev.deleted_count || 0) },
    ];

    const receipt = await irys.upload(data, { tags });
    const txId = receipt.id;
    const arweaveUrl = gatewayUrl(txId);

    console.log(`[uploader] 업로드 완료(${NETWORK}): ${arweaveUrl}`);
    res.json({ txId, arweaveUrl, network: NETWORK, permanent: IS_PERMANENT });
  } catch (err) {
    console.error("[uploader] 업로드 실패:", err.message);
    res.status(500).json({ error: err.message });
  }
});

app.get("/health", (_req, res) => res.json({ ok: true, network: NETWORK }));

const PORT = parseInt(process.env.PORT || "3000");
app.listen(PORT, () => {
  console.log(`[uploader] Irys 업로더 준비 완료 (${NETWORK}) → http://0.0.0.0:${PORT}`);
  if (!IS_PERMANENT) {
    console.warn(
      `[uploader] ⚠️  IRYS_NETWORK=${NETWORK} (테스트넷) — 업로드 데이터는 영구 저장이 아니며 ` +
      `약 60일 후 삭제됩니다. 진짜 영구 박제는 IRYS_NETWORK=mainnet 가 필요합니다.`
    );
  }
});
