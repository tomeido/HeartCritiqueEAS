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

// 멱등 조회용 GraphQL. mainnet(Arweave/Irys)은 태그 인덱싱을 지원해 동일
// App-Name+Story-Id 의 기존 박제를 찾을 수 있다. devnet 인덱서는 태그 검색을 제대로
// 지원하지 않으므로(=조회 0건) fail-open 으로 그냥 업로드한다(테스트넷은 실비용 없음).
// 운영자가 다른 엔드포인트를 쓰려면 IRYS_GRAPHQL_URL 로 덮어쓸 수 있다.
const GRAPHQL_URLS = {
  devnet:  "https://devnet.irys.xyz/graphql",
  mainnet: "https://uploader.irys.xyz/graphql",
};
const GRAPHQL_URL = process.env.IRYS_GRAPHQL_URL || GRAPHQL_URLS[NETWORK] || GRAPHQL_URLS.devnet;

// 같은 Story-Id 가 이 프로세스에서 동시에 업로드 중이면 결과를 공유(동시 이중 업로드 방지).
const _inFlight = new Map();

// 이미 같은 App-Name+Story-Id 로 박제된 tx 가 있으면 그 id 반환(없거나 조회 실패 시 null).
async function findExistingTx(storyId) {
  if (!storyId) return null;
  const query =
    `query($s:String!){transactions(tags:[` +
    `{name:"App-Name",values:["Heart-Critique"]},` +
    `{name:"Story-Id",values:[$s]}],order:ASC,first:1){edges{node{id}}}}`;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch(GRAPHQL_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, variables: { s: String(storyId) } }),
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    if (!r.ok) return null;
    const j = await r.json();
    const edges = j?.data?.transactions?.edges;
    return edges && edges.length ? edges[0].node.id : null;
  } catch (e) {
    console.warn("[uploader] 멱등 조회 실패(무시하고 업로드):", e.message);
    return null; // fail-open
  }
}

async function doUpload(body) {
  const storyId = String(body.payload?.story?.id || "");

  // 1차 멱등: 이미 박제됐으면 재업로드(=ETH 재지불) 없이 기존 tx 재사용.
  const existing = await findExistingTx(storyId);
  if (existing) {
    console.log(`[uploader] 중복 박제 회피 — 기존 tx 재사용(${NETWORK}): ${existing}`);
    return { txId: existing, arweaveUrl: gatewayUrl(existing),
             network: NETWORK, permanent: IS_PERMANENT, deduped: true };
  }

  const irys = await getIrys();
  const data = JSON.stringify(body);
  const ev = body.payload?.evidence || {};
  const tags = [
    { name: "Content-Type",  value: "application/json" },
    { name: "App-Name",      value: "Heart-Critique" },
    { name: "App-Version",   value: "6.0" },
    { name: "Category",      value: body.payload?.story?.category || "unknown" },
    { name: "Vote-Count",    value: String(body.payload?.votes?.count || 0) },
    { name: "Signed-By",     value: body.publicKey?.slice(0, 20) || "" },
    { name: "Story-Id",      value: storyId },
    { name: "Gap-Score",     value: String(ev.gap_score || "none") },
    { name: "Deleted-Count", value: String(ev.deleted_count || 0) },
  ];

  const receipt = await irys.upload(data, { tags });
  const txId = receipt.id;
  const arweaveUrl = gatewayUrl(txId);
  console.log(`[uploader] 업로드 완료(${NETWORK}): ${arweaveUrl}`);
  return { txId, arweaveUrl, network: NETWORK, permanent: IS_PERMANENT };
}

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
    const storyId = String(body.payload?.story?.id || "");

    // 동일 Story-Id 가 진행 중이면 그 업로드 결과를 공유(동시 이중 업로드/이중 지불 방지).
    if (storyId && _inFlight.has(storyId)) {
      const out = await _inFlight.get(storyId);
      return res.json(out);
    }

    const task = doUpload(body);
    if (storyId) _inFlight.set(storyId, task);
    try {
      const out = await task;
      res.json(out);
    } finally {
      if (storyId) _inFlight.delete(storyId);
    }
  } catch (err) {
    console.error("[uploader] 업로드 실패:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// 온체인 지갑 ETH 잔액(eth_getBalance, wei) — 후원이 들어오는 주소의 실시간 잔액.
// Irys '로드된 잔액'(getLoadedBalance)은 박제에 미리 충전된 선불 잔액이라 별개로 함께 보여준다.
async function getWalletWei(address) {
  const url = RPC_URLS[NETWORK] || RPC_URLS.devnet;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "eth_getBalance",
                             params: [address, "latest"] }),
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    const j = await r.json();
    return BigInt(j?.result || "0x0");
  } catch (e) {
    clearTimeout(timer);
    console.warn("[uploader] eth_getBalance 실패:", e.message);
    return null;
  }
}

const weiToEth = (wei) => (wei === null ? null : (Number(wei) / 1e18).toFixed(8));

// 박제 비용·지갑 잔액·후원 주소 실시간 조회. network 무관 동작(devnet/mainnet 자동).
// 누구나 이 address 로 후원(ETH 전송)하면 박제 자금이 충전된다.
app.get("/wallet", async (_req, res) => {
  try {
    const irys = await getIrys();
    const address = irys.address;
    // 박제 비용: 대표 크기들의 Irys 가격(atomic) → ETH 환산. 박제 1건은 보통 1~10KB 번들.
    const priceFor = async (bytes) => {
      try {
        const p = await irys.getPrice(bytes);
        return { bytes, atomic: p.toString(), eth: irys.utils.fromAtomic(p).toString() };
      } catch (e) {
        return { bytes, error: e.message };
      }
    };
    const [loadedAtomic, walletWei, p1kb, p10kb] = await Promise.all([
      irys.getLoadedBalance().catch(() => null),   // Irys 선불 잔액(atomic)
      getWalletWei(address),                         // 온체인 ETH(wei)
      priceFor(1024),
      priceFor(10 * 1024),
    ]);
    res.json({
      network: NETWORK,
      permanent: IS_PERMANENT,
      token: "ethereum",
      donation_address: address,            // 후원받을 주소(= Irys 자금 지불 지갑)
      wallet_eth: weiToEth(walletWei),      // 온체인 잔액(후원이 들어오는 곳)
      irys_balance: loadedAtomic !== null
        ? irys.utils.fromAtomic(loadedAtomic).toString() : null,  // 박제에 쓸 선불 잔액
      irys_balance_atomic: loadedAtomic !== null ? loadedAtomic.toString() : null,
      price_per_1kb: p1kb,                  // 박제 비용(대표 크기)
      price_per_10kb: p10kb,
      explorer: NETWORK === "mainnet"
        ? `https://etherscan.io/address/${address}`
        : `https://sepolia.etherscan.io/address/${address}`,
    });
  } catch (err) {
    console.error("[uploader] /wallet 실패:", err.message);
    res.status(500).json({ error: err.message, network: NETWORK });
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
