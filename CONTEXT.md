# Project: Heart & Critique (EAS-free Web2.5 Edition)

## 1. Project Overview & Philosophy
이 프로젝트는 인터넷의 수많은 홍보(PR) 노이즈와 마케팅 찌라시 속에서 날것의 사실을 건져 올리는 **AI 사냥개(Scout/Critic)**와, 그 정보의 역사적 가치를 최종 판결하는 **인간 문지기(Gatekeeper)**가 결합한 온체인 타임캡슐 아카이브입니다.
- **Goal:** 돈(투기성 토큰) 때문에 오염되는 예측 시장이나 대기업의 자본력에 삭제되는 Web2 커뮤니티의 사각지대를 해결합니다.
- **Core Value:** 인간 유저에게 지갑 연동, 가스비 결제 같은 Web3 진입 장벽을 강요하지 않으면서도, 소셜 로그인을 거친 인간의 '결단(투기 없는 순수한 가치 판단)'을 아위브(Arweave)에 영구 박제하여 데이터의 소유권과 영속성을 지킵니다.

---

## 2. Target Architecture (Web2.5 Hybrid)
유저의 UX는 완벽한 Web2 스타일을 따르고, 백엔드에서 AI 에이전트가 자율적으로 온체인 트랜잭션과 가스비를 처리합니다.

```text
[User] OAuth Social Login (Google/Discord) ➔ Vote 'Approve' (공론화 찬성)
   │
   ▼
[Backend/DB] Supabase 테이블에 투표수 누적 & 중복 체크
   │
   ▼
[Trigger] 특정 사건의 'Approve' 투표수가 임계값(Threshold)을 달성하는 순간
   │
   ▼
[Crypto Agent Worker] 
   1. 해당 사건 데이터 + 인간 투표 로그를 JSON 데이터셋으로 묶음
   2. 에이전트의 프라이빗 키로 데이터셋에 암호화 서명(Sign) 추가
   3. Irys(구 Bundlr) SDK를 사용하여 Arweave에 영구 박제 (가스비 대납)
   │
   ▼
[Frontend UI] 박제가 완료되면 아위브 트랜잭션 ID(Tx ID) 링크를 화면에 영구 노출