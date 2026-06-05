-- 마이그레이션 004: 구버전 박제 행의 arweave_url 정정 (BUG: 박제 링크 미연결)
--
-- 배경: 초기 uploader 는 네트워크와 무관하게 https://arweave.net/<txId> 를 저장했다.
--   - devnet 데이터는 arweave.net 에서 조회되지 않고(devnet.irys.xyz 에만 존재) → 링크 404.
--   - 현재 uploader 는 mainnet 도 gateway.irys.xyz(정산 전에도 즉시 서빙)를 1차 링크로 쓴다.
-- 프론트엔드는 이제 txId+network 로 링크를 재구성해 UI 는 즉시 정상 동작하지만,
-- RSS 피드(feed.py)는 저장된 arweave_url 을 그대로 노출하므로 DB 값도 정정해 둔다.
--
-- ⚠️ 운영 네트워크에 맞는 문장만 실행하라. (DB 에는 네트워크 정보가 없어 자동 분기 불가)
-- 안전: 실제 tx 가 박힌 행만 대상(__pending__ 선점 마커·NULL 제외). 재실행해도 멱등.

-- ── [A] devnet 운영 (IRYS_NETWORK=devnet, 기본값) ─────────────────────────────
--     arweave.net → devnet.irys.xyz 로 정정
update public.stories
   set arweave_url = 'https://devnet.irys.xyz/' || arweave_tx_id
 where arweave_url like 'https://arweave.net/%'
   and arweave_tx_id is not null
   and arweave_tx_id <> '__pending__';

-- ── [B] mainnet 운영 (IRYS_NETWORK=mainnet) ──────────────────────────────────
--     위 [A] 를 실행하지 말고 아래를 실행. (mainnet arweave.net 은 정산 후엔 동작하므로
--     선택 사항이나, gateway.irys.xyz 로 통일하면 박제 직후 404 창이 사라진다.)
-- update public.stories
--    set arweave_url = 'https://gateway.irys.xyz/' || arweave_tx_id
--  where arweave_url like 'https://arweave.net/%'
--    and arweave_tx_id is not null
--    and arweave_tx_id <> '__pending__';
