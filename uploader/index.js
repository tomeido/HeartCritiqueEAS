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

    const tags = [
      { name: "Content-Type",    value: "application/json" },
      { name: "App-Name",        value: "Heart-Critique" },
      { name: "App-Version",     value: "6.0" },
      { name: "Category",        value: body.payload?.story?.category || "unknown" },
      { name: "Vote-Count",      value: String(body.payload?.votes?.count || 0) },
      { name: "Signed-By",       value: body.publicKey?.slice(0, 20) || "" },
    ];

    const receipt = await irys.upload(data, { tags });
    const txId = receipt.id;
    const arweaveUrl = `https://arweave.net/${txId}`;

    console.log(`[uploader] 업로드 완료: ${arweaveUrl}`);
    res.json({ txId, arweaveUrl });
  } catch (err) {
    console.error("[uploader] 업로드 실패:", err.message);
    res.status(500).json({ error: err.message });
  }
});

app.get("/health", (_req, res) => res.json({ ok: true, network: NETWORK }));

const PORT = parseInt(process.env.PORT || "3000");
app.listen(PORT, () => {
  console.log(`[uploader] Irys 업로더 준비 완료 (${NETWORK}) → http://0.0.0.0:${PORT}`);
});
