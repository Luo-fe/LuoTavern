# TencentDB-Agent-Memory Research (primary sources)

> Research date: 2026-08-16. All claims below are followed to the source that owns them: the official GitHub repository (TencentCloud/TencentDB-Agent-Memory), the official Tencent Cloud documentation, PyPI, and registry/API metadata. Secondary blog posts were not used. Citation keys [S#] map to the **Source list** at the end of this document.

## TL;DR

- **TencentDB-Agent-Memory is a team-level memory hub for AI agents, not a PostgreSQL/TBase product.** It is the open-source companion of Tencent Cloud's managed **Agent Memory (TencentDB Agent Memory / 云数据库 Agent Memory)** service. The cloud service is built on **Tencent Cloud VectorDB + file storage**, not on TBase or PostgreSQL [S13][S24]. The open-source repo ships four components: **MemoryCore** (memory + metadata gateway), **MemoryKnowledge** (Wiki/CodeGraph knowledge service), **MemoryPanel** (Memory Hub UI), and **MemoryProxy** (transparent LLM proxy) [S1][S3][S4].
- **Memory model:** a four-layer pyramid **L0 conversation → L1 atomic memory → L2 scenario → L3 core/persona**, plus **short-term context management** (window management, cold-data offload, key-state retention) and **team memory** (memory assets: Chat Memory, Skill, Wiki, CodeGraph) [S1][S13]. The legacy preview SDK additionally modeled **actor / session / event / record** with `persona` and `episodic` record types; a `semantic`/`procedural` taxonomy is not present in the primary sources found [S21][S22].
- **Fully local mode exists.** Standalone MemoryCore uses **SQLite + local files + in-process state**, defaults to **BM25 retrieval without embeddings**, and needs only **Node.js ≥ 22.16 and an OpenAI-compatible LLM API** [S3][S10]. TCVDB/COS/Redis are only required in service mode or the managed cloud service [S5][S12][S19].
- **No OpenAI-compatible `/v1/memory/*` endpoints exist.** MemoryCore exposes its own REST surface (`/capture`, `/recall`, `/search/*`, `/v2/*`, `/v3/*`). MemoryProxy transparently forwards OpenAI `/v1/chat/completions` and Anthropic `/v1/messages`, and bridges memory tools via `/memory-bridge/**` [S3][S4]. The repo's MCP server exposes only **knowledge query tools** (Wiki + CodeGraph), not chat-memory search [S7].
- **Python SDK:** `from tencentdb_agent_memory import MemoryClient` (v2 default) and `from tencentdb_agent_memory.v3 import MemoryClient` (strict team/user/agent/session isolation). Sync and async variants exist; dependency is `httpx` [S8][S9][S15]. **As of 2026-08-16 the SDK is not on PyPI** (the four plausible distribution names return 404) [S25]; official docs instruct installing a local `.whl` [S15][S20].
- **For the QQ/Discord chatbot:** keep the current local backend as fallback, deploy standalone MemoryCore (SQLite) locally, and call it through the Python SDK's v3 client (`add_conversation` / `search_atomic` / `read_core` / `list_scenarios`). Point the existing OpenAI-compatible bot at MemoryProxy only if you want zero-code memory injection [S2][S4][S16].

## Deployment matrix

| Mode | Components | Storage | External dependencies | Fully local? | Published images / ports |
|---|---|---|---|---|---|
| **Standalone (open-source, single node)** | MemoryCore gateway only | SQLite (vectors + records) + local files; L2/L3 as markdown/JSONL | None beyond an OpenAI-compatible LLM API; embeddings optional (BM25 default) | **Yes** | Build `MemoryCore/Dockerfile` (`tencentdb-agent-memory`), or run `node --import tsx src/gateway/server.ts`. Port `8420` [S3][S10][S12] |
| **Full three-in-one (open-source, local)** | MemoryCore + MemoryKnowledge + MemoryPanel + MemoryProxy | SQLite/local files by default | Two LLM groups (`MEMORY_LLM_*`, `PROXY_UPSTREAM_*`); Redis/COS optional via config | **Yes** (Redis optional for Proxy) | Docker Hub `agentmemory/memory-core`, `agentmemory/memory-hub`, `agentmemory/memory-proxy` (amd64+arm64). Ports `8420` / panel `8125` / knowledge `8424` / proxy `8096` [S2][S6][S30] |
| **Service / cloud-hosted (Tencent Cloud managed)** | Managed memory instance (V3 API) | Tencent Cloud VectorDB + file storage | Tencent Cloud account, console-created instance, API key | No (managed cloud) | Console only; currently **Guangzhou region only**; data-plane endpoint `https://memory.tdai.tencentyun.com` (SDK docs) [S13][S19][S15] |
| **Self-hosted service mode (open-source)** | MemoryCore gateway, N replicas | TCVDB (vector) + COS (files) + Redis (state/queue) | Redis, Tencent Cloud VectorDB credentials (`VDB_*`), COS credentials (`COS_*`), or a Shark config service | No | K8s/TKE manifest `MemoryCore/deploy/k8s/tdai-memory.yaml`; `TDAI_DEPLOY_MODE=service` [S5][S12] |

**Minimum requirements (standalone):** Node.js ≥ 22.16.0, npm, and an OpenAI-compatible LLM API. Read-only queries may not invoke an LLM, but memory extraction/aggregation (L1→L3) requires valid LLM credentials [S3]. The standalone Docker image is `node:22-slim`-based (~920 MB), runs as uid `10001`, PID 1 `tini`, port `8420` [S12].

**Is it localhost-capable?** Yes. Standalone binds `127.0.0.1:8420`, stores data under `~/.memory-tencentdb/memory-tdai`, and uses BM25-only retrieval when embeddings are disabled [S3][S10]. The full local stack also runs on localhost via `deploy/global-images/start-all.sh` and opens the panel at `http://localhost:8125` [S1][S2].

## Memory model

### Four-layer long-term pyramid (L0–L3)

| Layer | Name | Contents | Retrieval use |
|---|---|---|---|
| **L0** | Conversation / Raw Log | Raw messages, tool-call logs, full context | Verify exact wording, timestamps, provenance; `/v3/conversation/*` [S1][S14] |
| **L1** | Atomic Memory | Extracted facts, preferences, constraints, events; each carries a source link | Precise recall; `/v3/atomic/*` [S1][S14] |
| **L2** | Scenario / Scene Block | Knowledge blocks organized by project/scenario (file-system style, e.g. `工作.md`) | Restore working context; `/v3/scenario/*` [S1][S14] |
| **L3** | Core / Persona | Long-term profile, stable patterns, high-level cognition | Context bootstrap; `/v3/core/*` [S1][S14] |

Generation is asynchronous: write L0, then the pipeline distills L0→L1→L2→L3. Retrieval is hybrid (BM25 + vector + RRF), with caps on result count, character budget, and timeout [S1][S5]. The cloud docs call the four layers 原始对话 / 原子记忆 / 场景记忆 / 核心记忆 and add **short-term memory** (context-window management, cold-data offload, key-state retention) as the entry point into long-term memory [S13][S16].

### Core concepts (current V3 API)

The V3 API organizes memory along a scope chain: **team_id → user_id → agent_id → session_id**, plus an orthogonal **task_id**. All dimensions are optional in the HTTP API (they progressively narrow scope); the Python v3 SDK requires `team_id`/`agent_id`/`user_id` at construction and requires `session_id` for writes [S14][S9][S16]. Memory extraction is keyed by `agent_id`; `team_id`/`user_id` are auxiliary tags in the managed cloud SDK guidance [S16].

Mapping to the terminology in the request: **actor ≈ agent_id** (the agent whose memory is being written), **session ≈ session_id**, **events ≈ conversation messages** appended via `add_conversation`/L0, **memory blocks/records ≈ L1 atomic memories** (and legacy preview records), and **contexts ≈ L2 scenario blocks plus the injected `<session_context>`** used by MemoryProxy. There is no first-class contexts API; the closest concepts are L2 scenarios and the prompt-side session context [S1][S4][S14].

### Legacy preview taxonomy (discontinued)

The retired 预览版（已停售） SDK `tdaimemory` used `actor_id` / `create_session()` / `append_event()` / `search_records(type=...)`. Its documented record types were **persona** (画像记忆) and **episodic** (事件记忆), with custom strategies allowed; no built-in semantic or procedural type was documented in the primary pages reviewed [S21][S22]. The current V3 management API `RecordType` includes `conversation`, `memory`, `scene`, `persona`, `memory/persona`, `memory/episodic`, and `memory/instruction` [S23].

## API / SDK reference

### HTTP surface (open-source MemoryCore)

| Method/path | Purpose | Status |
|---|---|---|
| `GET /health` | health check | public |
| `POST /capture` | write a conversation turn (L0) | compatibility |
| `POST /recall` | memory recall | compatibility |
| `POST /search/memories` | L1 memory search | compatibility |
| `POST /search/conversations` | L0 conversation search | compatibility |
| `POST /session/end` | end/flush session | compatibility |
| `POST /v2/conversation/*`, `/v2/atomic/*`, `/v2/scenario/*`, `/v2/core/*` | v2 data plane | stable |
| `POST /v3/conversation/*`, `/v3/atomic/*`, `/v3/scenario/*`, `/v3/core/*` | strongly-isolated v3 data plane | recommended for new integrations |
| `/v3/skill/*` | Skill management/search/versions/extraction | stable |
| `/v3/meta/*` | users/teams/agents/tasks/assets/ACL | management plane |
| `/v3/knowledge/*` | knowledge asset metadata registration | management plane |

Source: [S3]. Direct compatibility endpoints use simple JSON, e.g. `POST /capture` accepts `{user_content, assistant_content, session_key, session_id?, user_id?, messages?}` and `POST /recall` accepts `{query, session_key, user_id?}` returning `{context, strategy, memory_count, code, message}` [S28].

**Auth:** when `TDAI_GATEWAY_API_KEY` is set, every endpoint except `/health`/CORS preflight requires `Authorization: Bearer <key>` and `x-tdai-service-id: <memory-instance-id>`. The v3 data plane additionally expects `team_id`/`agent_id`/`user_id` in the body or `x-tdai-*` headers [S3]. MemoryProxy validates `x-tdai-user-key` against `POST /v3/meta/auth/verify` and derives `user_id` [S4].

### OpenAI-compatible endpoints

There is **no `/v1/memory/*` memory API**. The OpenAI compatibility is only for chat proxying: MemoryProxy forwards `/v1/chat/completions` (OpenAI) and `/v1/messages` (Anthropic) verbatim, injecting memory/skills/knowledge around the request [S4]. It also reverse-proxies MemoryCore tools through `/skill-bridge/**` and `/memory-bridge/**` [S4]. Do not expect a Mem0-style `/v1/memories` interface here.

### MCP server mode

MemoryKnowledge ships an MCP **stdio** server (`KNOWLEDGE_API_URL=http://localhost:8421 node dist/mcp/server.js`, entry `MemoryKnowledge/bin/mcp.mjs`). It exposes **12 query tools**: 8 CodeGraph tools (`code_search`, `code_explore`, `code_callers`, `code_callees`, `code_impact`, `code_node`, `code_status`, `code_files`) and 4 Wiki tools (`wiki_search`, `wiki_read`, `wiki_list`, `wiki_graph`). Management operations are deliberately not exposed as MCP tools [S7]. There is no MCP server for MemoryCore chat memory in this repo.

### Python SDK

**Import names / classes (open-source SDK):**

- `tencentdb_agent_memory.MemoryClient` — v2 client (default export), sync.
- `tencentdb_agent_memory.AsyncMemoryClient` — v2 async.
- `tencentdb_agent_memory.v3.MemoryClient` / `AsyncMemoryClient` — v3 strict-isolation data plane.
- `tencentdb_agent_memory.v3.SkillClient`, `MetadataClient`, `MemoryPromptClient`, `MemoryGenerationLogClient` — v3 management/skill/prompt clients.
- Errors: `TDAMError`, `ParamError` [S8][S9][S15].

**Install (not on PyPI as of 2026-08-16):**

- Official cloud docs: download `tencentdb_agent_memory_sdk_python-0.2.0-py3-none-any.whl` and `pip install tencentdb_agent_memory_sdk_python-0.2.0-py3-none-any.whl` [S15].
- Repo pyproject distribution name is `tencentdb-agent-memory-sdk-python-v2` (version `1.0.1-beta.1`), while the repo README says PyPI name `tencentdb-agent-memory-sdk-python` [S8][S9]. PyPI returns 404 for `tencentdb-agent-memory-sdk-python`, `tencentdb-agent-memory-sdk-python-v2`, `tencentdb-agent-memory-sdk`, and `tdaimemory` [S25]. Local install: `cd sdk/memory-core/python && python -m build` (or `pip install .`); dependency is `httpx>=0.24.0`; `requires-python >=3.9` [S8][S9].

### Minimal end-to-end example (Python, v3)

From the official SDK docs and repo README [S8][S15][S16]:

```python
from tencentdb_agent_memory.v3 import MemoryClient

client = MemoryClient(
    endpoint='http://127.0.0.1:8420',      # standalone MemoryCore; cloud: https://memory.tdai.tencentyun.com
    api_key='your-api-key',                # Gateway bearer key, or cloud Memory instance API key
    service_id='default',                  # memory instance id / x-tdai-service-id
    team_id='team-abc123',
    agent_id='agt-xyz789',
    user_id='usr-456',
    session_id='agent-main:sess-001',      # optional at construction; required for writes
)

# 1) append a conversation (L0); server distills L1/L2/L3 asynchronously
result = client.add_conversation(messages=[
    {'role': 'user', 'content': '帮我查一下上周的会议纪要'},
    {'role': 'assistant', 'content': '好的，根据记忆……'},
])
print(result)  # {'accepted_ids': [...], 'accepted_versions': [...], 'total_count': 2}

# 2) recall/search memory
l1 = client.search_atomic(query='会议纪要', limit=5)   # L1 atomic memories
core = client.read_core()                             # L3 persona text
scenarios = client.list_scenarios()                   # L2 scenario file index

print(l1['items'])       # each item has type (fact/preference/instruction) + content
print(core['content'])
print(scenarios['entries'])

client.close()
```

The v2 client works the same way without mandatory team/user/agent (`client.add_conversation(session_id=..., messages=[...])`, `client.search_atomic(query=..., limit=...)`, `client.read_core()`, `client.list_scenarios(path_prefix='')`) [S8]. An async-context-manager pattern is recommended for an existing bot event loop [S16].

## Integration recipe for an existing chat app

For a Python 3.12 Discord/QQ bot (e.g. `deepdiscord`), the lowest-risk path is a **local fallback + optional cloud/self-hosted primary**:

1. **Run MemoryCore standalone locally.** `cd MemoryCore && npm install && npm run build`, then export `TDAI_LLM_API_KEY`/`TDAI_LLM_BASE_URL`/`TDAI_LLM_MODEL` and start the gateway (`node --import tsx src/gateway/server.ts`). It listens on `127.0.0.1:8420` and stores SQLite data under `~/.memory-tencentdb/memory-tdai` [S3][S10]. Optionally enable vector search with an OpenAI-compatible embedding model in `memory.embedding` [S10].
2. **Install the SDK from source.** It is not on PyPI yet; build the wheel from `sdk/memory-core/python` (`python -m build`) and `pip install` it. Python 3.12 is supported by the package's `requires-python >=3.9` metadata [S8][S9].
3. **Add a memory wrapper around the bot's message handler.** Map one bot user to `user_id`, one bot persona/agent to `agent_id`, one Discord/QQ server or thread to `team_id`, and one conversation to `session_id`. After each assistant reply, call `add_conversation(...)`; before calling the LLM, call `search_atomic(query=user_message, limit=5)`, `read_core()`, and `list_scenarios()` in parallel (`asyncio.gather`) and prepend the L1 hits / append the L2/L3 block to the prompt [S16].
4. **Keep the current local files as fallback.** Write the same messages to the existing markdown/SQLite/vector store first, then try the memory client; on `TDAMError`/connection failure, continue with the local store. MemoryCore standalone itself is SQLite-based and can serve as the primary, so a second fallback is only needed if the gateway is unavailable [S3][S5].
5. **Alternative zero-code option.** Point an OpenAI-compatible chat client at MemoryProxy: `baseURL=http://127.0.0.1:8096/proxy/<spaceId>` (or `http://127.0.0.1:8096/dsh/default` for DeepSeek Harness), API key = a `sk-mem-...` user key. The proxy injects team/agent/task context and memory L2/L3, and exposes L0/L1 as read-only tools [S2][S4].

## Configuration & authentication

### Open-source environment variables (MemoryCore)

| Variable | Default | Meaning |
|---|---|---|
| `TDAI_GATEWAY_CONFIG` | auto-discovered | YAML/JSON config path |
| `TDAI_GATEWAY_HOST` | `127.0.0.1` | bind address |
| `TDAI_GATEWAY_PORT` | `8420` | gateway port |
| `TDAI_GATEWAY_API_KEY` | unset | Bearer auth; required for non-loopback binding |
| `TDAI_DATA_DIR` | `~/.memory-tencentdb/memory-tdai` | local data dir |
| `TDAI_LLM_API_KEY` | empty | LLM API key |
| `TDAI_LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible LLM URL |
| `TDAI_LLM_MODEL` | `gpt-4o` | LLM model |
| `TDAI_DEPLOY_MODE` | `standalone` | `standalone` / `service` |
| `STATE_BACKEND` | auto | `local` / `redis` |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` / `REDIS_KEY_PREFIX` | see source | Redis for service mode |
| `VDB_ENDPOINT` / `VDB_USER` / `VDB_API_KEY` / `VDB_DATABASE` | see source | Tencent Cloud VectorDB for service mode |
| `COS_SECRET_ID` / `COS_SECRET_KEY` / `COS_TOKEN` / `COS_URL` / `COS_PATH_PREFIX` | see source | COS object storage for service mode |
| `SHARK_BASE_URL` | — | Shark dynamic config service |
| `MEMORY_MAX_BODY_BYTES` | 1 MiB | request body size cap |
| `TDAI_SKILL_ENABLED` | file config | force-enable Skill module |

Sources: [S3][S5][S12][S29]. The deployment scripts also use `MEMORY_LLM_BASE_URL`/`MEMORY_LLM_API_KEY`/`MEMORY_LLM_MODEL`/`MEMORY_LLM_PROTOCOL` and `PROXY_UPSTREAM_URL`/`PROXY_UPSTREAM_API_KEY`/`PROXY_UPSTREAM_MODEL` for the full stack [S6].

**Tencent Cloud credentials:** the open-source stack does **not** use `TENCENTCLOUD_SECRET_ID`/`TENCENTCLOUD_SECRET_KEY`. Service mode uses `VDB_*` and `COS_SECRET_ID`/`COS_SECRET_KEY` instead [S5][S29]. The managed cloud **data plane** uses a per-instance **API Key (`sk-...`) + instance ID + endpoint** obtained from the console, not SecretId/Key [S20][S15]. The managed cloud **management plane** (`Host: tdai.tencentcloudapi.com`) is a standard Tencent Cloud API 3.0 interface (`Action`, `Version`, `Region`, `X-TC-Action`), so it uses Tencent Cloud API signing rather than the data-plane API key [S23].

### Cloud pricing / free tier

- Managed **Agent Memory** is pay-as-you-go (按量计费), billed hourly: **memory storage ¥0.02 per 10,000 memory entries per hour** + **model calls ¥0.01 per Credit** (Credit = normalized LLM token cost) [S17][S18].
- Default limit: **20 million long-term memory entries** per instance; exceeding it blocks new writes but not reads. Credit has no cap [S17].
- Whitelisted accounts kept a free trial (包年包月免费态) only until **2026-08-15**; no new free-tier applications [S17][S18][S19].
- Currently **only the Guangzhou region** is open; a single Tencent Cloud account can create at most **500 Memory instances** [S19].
- The management API example `DescribeMemoryPlusRecord` has a default rate limit of **20 requests/second** [S23].

## Known limitations / pitfalls

1. **It is not TBase.** No repository or official page references 云数据库 TBase or PostgreSQL as storage. TencentDB here is the database-team brand; storage is SQLite (standalone) or **Tencent Cloud VectorDB + COS** (service/cloud). Do not provision TBase for this project [S3][S5][S13][S24]. (A grep of the cloned repo found no TBase/PostgreSQL storage references.)
2. **No OpenAI-compatible `/v1/memory/*` API.** If you need Mem0-style endpoints, wrap MemoryCore's REST/SDK yourself; the only OpenAI compatibility is chat-completion proxying [S4][S3].
3. **Python SDK is not yet on PyPI** (404 for all plausible names as of 2026-08-16) and the repo/cloud docs disagree on the distribution name (`tencentdb-agent-memory-sdk-python` vs `tencentdb-agent-memory-sdk-python-v2` vs `tencentdb-agent-memory-sdk`). Install from the repo wheel/source [S8][S9][S15][S25].
4. **API churn.** The repo default branch is `feat/server_team` (v2.0.0, Team Memory Beta), and the cloud docs label the older `tdaimemory` preview SDK 预览版（已停售）. v2 vs v3 differ in mandatory isolation fields; new integrations should use v3 [S14][S21][S26].
5. **Embedding defaults.** Standalone defaults to `memory.embedding.provider: 'none'` (BM25 only, `bm25.language: 'zh'`). Vector search needs either an OpenAI-compatible embedding endpoint or a local `node-llama-cpp` `embeddinggemma-300m` model (~300 MB, 768-dim, ~512-char CJK-safe input truncation). TCVDB service mode can use server-side `bge-large-zh` [S10][S11][S5].
6. **LLM dependency.** L0 write is cheap, but L1/L2/L3 extraction requires a valid LLM API; BM25 retrieval works without embeddings but aggregation does not. The full stack needs **two** LLM configurations (memory group + proxy group) [S3][S6].
7. **License inconsistency.** Root `LICENSE` is MIT [S27], but the older `README.docker.md` says Proprietary — Tencent Cloud [S12] and GitHub reports `NOASSERTION` [S26]. For redistribution, rely on the LICENSE file, but note the contradiction.
8. **MCP coverage.** The MCP server only exposes Wiki/CodeGraph query tools, not chat-memory read/write; management operations are not exposed [S7].
9. **Language/limits.** BM25 default language is Chinese (`zh`); local embedding truncates input to ~512 chars (CJK) / ~800 chars (Latin heuristic); CodeGraph currently prioritizes public HTTPS repositories (private/SSH support still being refined); Wiki/CodeGraph build asynchronously [S1][S10][S11].
10. **Cloud constraints.** Guangzhou-only, 500-instance cap, 20M-entry storage cap, and management-API rate limits (e.g. 20 req/s) documented above [S19][S17][S23]. The open-source Proxy adds its own configurable TPM/QPM rate limiting keyed by `spaceId × model` [S4].

## Comparison notes (one paragraph)

TencentDB-Agent-Memory is an **opinionated, team-oriented memory governance layer** rather than a minimal vector store: it distills conversations into a four-layer pyramid (L0→L3), adds Skills/Wiki/CodeGraph as governed memory assets, and enforces team/user/agent/ACL isolation and a transparent LLM proxy — so it is closer to LangChain/LlamaIndex memory + a memory backend + an asset/permission control plane than to a single library. Plain LangChain/LlamaIndex memory typically only gives you in-process buffers or thin vector-store wrappers with no distillation or sharing semantics, while Mem0 offers a focused OpenAI-compatible memory API (`/v1/memories`) with extraction/search but without this project's team-asset governance, Skill/CodeGraph/Wiki assets, or proxy-based zero-code agent injection [S1][S3][S4][S13].

## Source list

- [S1] GitHub repo root README — https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/README.md
- [S2] GitHub INSTALL.md — https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/INSTALL.md
- [S3] MemoryCore/README.md — https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/MemoryCore/README.md
- [S4] MemoryProxy/README.md — https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/MemoryProxy/README.md
- [S5] README.deployment.md — https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/README.deployment.md
- [S6] deploy/global-images/.env.example — https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/deploy/global-images/.env.example
- [S7] MemoryKnowledge README / .env.example / MCP source — https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/MemoryKnowledge/README.md , .../MemoryKnowledge/.env.example , .../MemoryKnowledge/src/mcp/server.ts , .../MemoryKnowledge/src/mcp/tools.ts
- [S8] Python SDK README — https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/sdk/memory-core/python/README.md
- [S9] Python SDK metadata/client — https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/sdk/memory-core/python/pyproject.toml , .../tencentdb_agent_memory/__init__.py , .../tencentdb_agent_memory/v3/client.py
- [S10] MemoryCore config templates — https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/MemoryCore/tdai-gateway.standalone.yaml , .../MemoryCore/tdai-gateway.yaml
- [S11] Embedding implementation — https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/MemoryCore/src/core/store/embedding.ts
- [S12] README.docker.md — https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/README.docker.md
- [S13] Tencent Cloud: Memory 介绍 — https://cloud.tencent.com/document/product/1813/132100
- [S14] Tencent Cloud: API 简介 (V3 memory model) — https://cloud.tencent.com/document/product/1813/132001
- [S15] Tencent Cloud: Python SDK 简介 — https://cloud.tencent.com/document/product/1813/135117
- [S16] Tencent Cloud: 自研 Agent 接入指引 — https://cloud.tencent.com/document/product/1813/132103
- [S17] Tencent Cloud: 计费概述 — https://cloud.tencent.com/document/product/1813/133512
- [S18] Tencent Cloud: 计费定价 — https://cloud.tencent.com/document/product/1813/133513
- [S19] Tencent Cloud: 新建 Memory 实例 — https://cloud.tencent.com/document/product/1813/132110
- [S20] Tencent Cloud: 接入准备 — https://cloud.tencent.com/document/product/1813/132102
- [S21] Tencent Cloud: 快速体验（预览版已停售） — https://cloud.tencent.com/document/product/1813/123380
- [S22] Tencent Cloud: 快速召回（预览版已停售） — https://cloud.tencent.com/document/product/1813/123405
- [S23] Tencent Cloud: 查询正式版 Memory 实例记忆数据 (API) — https://cloud.tencent.com/document/product/1813/132185
- [S24] Tencent Cloud: Agent Memory product page — https://cloud.tencent.com/product/agm
- [S25] PyPI registry lookups (404 on 2026-08-16): tencentdb-agent-memory-sdk-python, tencentdb-agent-memory-sdk-python-v2, tencentdb-agent-memory-sdk, tdaimemory — https://pypi.org/pypi/tencentdb-agent-memory-sdk-python/json (and analogous URLs)
- [S26] GitHub repo metadata — https://api.github.com/repos/TencentCloud/TencentDB-Agent-Memory
- [S27] LICENSE — https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/LICENSE
- [S28] MemoryCore gateway request types — https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/MemoryCore/src/gateway/types.ts
- [S29] MemoryCore gateway config — https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/MemoryCore/src/gateway/config.ts
- [S30] deploy/dockerhub/README.md — https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/deploy/dockerhub/README.md