# SUMO Multi-Agent Traffic Control — Sườn triển khai POC

> Trạng thái: bản thiết kế **v0.2** · Ngày: 2026-09-09
> Stack: **Python 3.14** · SUMO 1.27 (traci/libsumo) · **OpenAI Responses API** · FastAPI · **PostgreSQL 17** · Docker
>
> Thay đổi so với v0.1: Python 3.12→3.14 · Claude→OpenAI · SQLite→PostgreSQL · quyết định không dùng LangChain (§13)

---

## 0. TL;DR đánh giá

**Ý tưởng khả thi và đáng làm.** Nó nằm đúng trên một hướng nghiên cứu đang hoạt động (LLM cho traffic signal control), SUMO là công cụ chuẩn, và toàn bộ scope chạy được trên một máy. Nhưng có **4 điểm sẽ giết POC nếu không xử lý ngay từ thiết kế**:

| # | Vấn đề | Hệ quả nếu bỏ qua | Cách xử lý |
|---|---|---|---|
| 1 | **Lệch pha thời gian**: SUMO 1 step = 1s mô phỏng (~ms thực); 1 lượt LLM mất 2–15s | Vòng lặp sim đứng chờ LLM → demo giật, không chạy nổi 1 giờ | Tách 2 vòng lặp (§3.2). Quyết định theo chu kỳ 60–120s mô phỏng, async, sim không bao giờ chờ |
| 2 | **Không có baseline** | Không chứng minh được LLM tốt hơn gì → POC không có kết luận | Bắt buộc 3 baseline: fixed-time, actuated, Max-Pressure (§7) |
| 3 | **LLM sinh trực tiếp thời lượng đèn** | Cắt pha vàng, bỏ đói hướng phụ, dao động hệ thống | Không gian hành động hẹp + **validator tất định chạy TRƯỚC supervisor** (§5) |
| 4 | **Dùng AI sinh thẳng `.net.xml`** | Ràng buộc chéo hình học/connection/tlLogic → XML hỏng, tốn nhiều ngày debug | AI sinh **scenario spec** (JSON); `netgenerate`/`randomTrips` sinh mạng (§4) |

Phần **observability** (ghi hội thoại, ghi quyết định, monitoring token) là phần có giá trị demo cao nhất và rẻ nhất để làm. Coi đó là sản phẩm chính, không phải phần phụ.

---

## 1. Đánh giá 4 quyết định kỹ thuật (v0.2)

### 1.1 Python 3.14 — ✅ Đồng ý, không có blocker

Đã kiểm chứng trên máy này (Ubuntu 24.04, `/usr/bin/python3.14` có sẵn):

| Gói | Kết quả kiểm tra |
|---|---|
| `libsumo` 1.27.1 | ✅ Có wheel **`cp314`** (cùng cp310–cp313) |
| `traci`, `sumolib` 1.27.1 | ✅ Pure Python (`py2.py3`) — chạy mọi phiên bản |
| `eclipse-sumo` 1.27.1 | ✅ Wheel `py3-none-manylinux_2_28_x86_64` — **đóng gói sẵn binary** `sumo`, `sumo-gui`, `netgenerate` |

**Phát hiện đáng giá**: vì `eclipse-sumo` ship binary qua wheel, **không cần PPA và không cần `apt install sumo`**. Toàn bộ SUMO cài bằng `pip` → Dockerfile đơn giản hơn hẳn và môi trường dev khớp môi trường container.

⚠️ **Một ngoại lệ đã gặp thực tế**: wheel không đóng gói kèm `libatomic.so.1`, mà cả 14 binary SUMO đều link tới nó — Ubuntu 24.04 và `python:3.14-slim` đều không có sẵn. Cần `apt install libatomic1` ở cả máy dev lẫn Dockerfile. Đây là gói hệ thống *duy nhất* còn thiếu (đã kiểm `ldd` toàn bộ binary).

Hai lưu ý:
- **Dùng bản build tiêu chuẩn (có GIL), không dùng free-threaded.** Workload này là I/O-bound (chờ HTTP tới OpenAI), `asyncio` giải quyết trọn vẹn; free-threading không thêm gì mà lại đưa `libsumo` (C++ extension) vào vùng rủi ro chưa được kiểm chứng.
- Môi trường: **`python3.14 -m venv .venv`** + `pip` (đã kiểm chứng chạy tốt trên máy này — Python 3.14.6, pip 26.1.2). Khoá phiên bản bằng **`pip-tools`**: khai báo ở `requirements.in`, compile ra `requirements.txt` pin đầy đủ và **commit vào git**. Đây không phải nghi thức thừa — dự án so sánh các run cách nhau nhiều tuần, nếu phiên bản SUMO hay thư viện trôi giữa chừng thì bảng kết quả mất giá trị.

### 1.2 OpenAI — ✅ Đồng ý, và với workload này chi phí tốt hơn đáng kể

Tôi đã tra lineup và bảng giá hiện hành (kiến thức nội tại của tôi tới 05/2026 đã lạc hậu — nay đã có GPT-6 Astra và họ GPT-5.6):

| Model | Input $/1M | Output $/1M | Cached input | Định vị |
|---|---|---|---|---|
| `gpt-6-astra` | $10.00 | $50.00 | $1.00 | Mạnh nhất |
| `gpt-5.6-sol` | $4.00 | $20.00 | $0.40 | Flagship |
| `gpt-5.6-terra` | $2.00 | $12.00 | $0.20 | Cân bằng |
| **`gpt-5.6-luna`** | **$0.20** | **$1.20** | **$0.02** | **Tối ưu cho tải lớn, nhạy chi phí** |

`gpt-5.6-luna` chính là thứ làm kiến trúc này rẻ đi: `JunctionAgent` là nguồn phát sinh chi phí chính (n_agents × n_cycles × n_rounds), và Luna đúng là hạng model cho loại tải đó. Chi phí/run giảm từ ~$7–8 (ước tính v0.1) xuống **~$2** (§11).

**Phân bổ model:**

| Vai trò | Model | `reasoning.effort` | Lý do |
|---|---|---|---|
| `JunctionAgent` | `gpt-5.6-luna` | `low` | Tải lớn, nhiệm vụ hẹp và có schema chặt |
| `SupervisorAgent` | `gpt-5.6-terra` | `medium` | 1 lần/chu kỳ, cần phán đoán giải xung đột |
| `ScenarioGenerator` | `gpt-5.6-sol` | `high` | Chạy offline 1 lần, chất lượng quan trọng hơn giá |

**Ba điều chỉnh thiết kế do đổi provider** (xem code §3.3):
1. Dùng **Responses API** (`client.responses.parse`), **không** dùng Chat Completions — OpenAI khuyến nghị cho dự án mới, và với reasoning model nó tận dụng cache tốt hơn rõ rệt.
2. **Prompt caching là tự động** (ngưỡng ≥1.024 token input), khác Anthropic phải gắn `cache_control` thủ công. GPT-5.6+ có thêm breakpoint tường minh. Dùng `prompt_cache_key` ổn định theo từng nút để request cùng prefix được route về cùng cache. Cache discount **90%** → prefix ổn định là tiền thật.
3. Kiểm chứng cache bằng `usage.input_tokens_details.cached_tokens` — nếu luôn bằng 0 thì có thứ gì đó đang phá prefix (timestamp, JSON không sort, tool list đổi thứ tự).

**Một khuyến nghị kiến trúc**: gom mọi lời gọi LLM vào **một hàm duy nhất** trong `agents/llm.py`. Đây không phải lớp trừu tượng hoá — chỉ là *một call site*. Bạn nói "cân nhắc" OpenAI, và POC sẽ chạy vài tuần; nếu sau này muốn thử provider khác, bạn sửa một file thay vì sửa mọi agent.

### 1.3 PostgreSQL — ✅ Đồng ý với quyết định, nhưng lý do không phải "SQLite nhỏ quá"

Cần nói thẳng về con số: 16 nút × lấy mẫu 10s × 1 giờ = **5.760 dòng/run**. SQLite xử lý gấp 1.000 lần con số đó không hề hấn gì. Dung lượng **không** phải lý do.

Lý do thật sự để chọn Postgres — và chúng đủ chính đáng:
- **Concurrency**: `sim` ghi liên tục trong khi `app`/dashboard đọc. SQLite ở chế độ WAL thì tạm được nhưng vẫn có single-writer; Postgres sạch hơn.
- **Chạy song song nhiều run** để so sánh baseline — 4 chế độ chạy cùng lúc, mỗi cái một writer.
- Bạn **đã có Docker Compose** rồi, thêm Postgres là ~10 dòng YAML.
- Kiểu dữ liệu tốt hơn: `JSONB` (query được vào trong payload hội thoại), `TIMESTAMPTZ`, index thật.

Hai điều kiện kèm theo:
- Dùng **SQLAlchemy 2.x + Alembic**, không viết SQL thô rải rác. Để **unit test chạy trên SQLite in-memory** — test không cần container, chạy trong mili giây.
- **Không dùng TimescaleDB ở POC.** Với 5.760 dòng/run thì hypertable là phức tạp hoá vô ích. Cân nhắc lại khi vượt ~10 triệu dòng.

### 1.4 LangChain — ❌ Không nên. LangGraph: chưa, nhưng có điều kiện rõ ràng

Đây là câu tôi có ý kiến dứt khoát.

**Việc cần làm thực tế là gì?** 6 agent, JSON vào/JSON ra có schema, truyền tin 2 vòng có giới hạn, một validator tất định, một supervisor. Viết bằng `asyncio` + Pydantic + SDK thì khoảng **200–250 dòng**, và bạn đọc hiểu được toàn bộ.

**LangChain trả về gì và lấy đi gì:**

| Được | Mất |
|---|---|
| Prompt template, output parser | Bạn đã có Pydantic + `responses.parse` — native, chặt hơn |
| Trừu tượng hoá model | Bạn đã có `agents/llm.py`, 1 file |
| Callback/tracing (LangSmith) | Lấy được từ Langfuse/OpenTelemetry mà không cần LangChain |
| — | **Che mất payload request thật** |
| — | Cây phụ thuộc nặng, API hay đổi giữa các version |
| — | Async debug qua nhiều lớp trừu tượng rất khó |

Dòng in đậm là dòng quyết định. Cache discount là **90%** và cache khớp theo **prefix** — chỉ cần một framework âm thầm dựng lại prompt theo thứ tự khác là hit rate về 0 và bạn trả giá gấp 10, mà không nhìn thấy nguyên nhân ở đâu. Ở dự án mà chi phí token là một trong các metric bạn phải theo dõi trên dashboard, mất quyền kiểm soát payload là cái giá quá đắt.

**LangGraph** là mảnh đúng hơn nhiều so với LangChain — nó sinh ra cho state machine đa agent. Nhưng orchestration của bạn ở Phase 2 là **pipeline cố định 4 vòng** (observe → coalition → validate → approve). Pipeline cố định chính là trường hợp mà framework đồ thị đóng góp ít nhất.

**Kết luận: dùng `asyncio` thuần ở Phase 2.** Chuyển sang LangGraph khi (và chỉ khi) chạm một trong ba mốc cụ thể sau:
1. Định tuyến trở nên **động** — số vòng trao đổi do model quyết định thay vì cố định
2. Cần **checkpoint/resume** giữa chu kỳ (tạm dừng sim, sửa tay, chạy tiếp)
3. Cần **human-in-the-loop** — người duyệt chen vào giữa validator và supervisor

Ba mốc này đều là Phase 4+. Khi đó `agents/llm.py` và các model Pydantic trong `protocol.py` vẫn dùng lại được nguyên vẹn — migration rẻ.

> Bối cảnh: khảo sát 2026 ghi nhận phần lớn team dùng multi-agent orchestration quá sớm, và trên 75% hệ multi-agent trở nên khó quản lý khi vượt 5 agent. Bạn đang ở 6 agent — đúng ngưỡng phải giữ mọi thứ đơn giản và quan sát được.

---

## 2. Quy mô đề xuất

| Hạng mục | Con số | Lý do |
|---|---|---|
| Nút giao có đèn | 12–16 | Đủ "lớn và đa dạng" |
| Nút giao **có agent** | 4–6 | Token/latency tăng tuyến tính. 6 là trần thực dụng |
| Loại nút | ngã tư, ngã ba T, vòng xuyến, nút lệch | Tính đa dạng |
| Thời lượng 1 run | 3.600s mô phỏng | ~40 chu kỳ quyết định |
| Chu kỳ quyết định | 90s mô phỏng | Đủ dài để LLM kịp, đủ ngắn để phản ứng |
| Nhu cầu | 3.000–8.000 trips/giờ | Đủ tạo tắc thật |

---

## 3. Kiến trúc

### 3.1 Sơ đồ

```
┌─────────────────────────────────────────────────────────────┐
│  sim_process (sở hữu DUY NHẤT kết nối TraCI)                │
│  ┌────────────┐  step()  ┌──────────────┐                   │
│  │ SUMO 1.27  │◄─────────│ SimRunner    │                   │
│  │ traci /    │─────────►│ - vòng lặp   │                   │
│  │ libsumo    │  state   │ - incidents  │                   │
│  └────────────┘          │ - áp lệnh    │                   │
│                          └──────┬───────┘                   │
└─────────────────────────────────┼───────────────────────────┘
              StateSnapshot ↓     │ ↑ ActionBatch (đã duyệt)
┌─────────────────────────────────┼───────────────────────────┐
│  orchestrator (asyncio thuần — KHÔNG LangChain)             │
│   ┌─────────────────────────────▼────────────────────────┐  │
│   │ Vòng 1: observe   — mỗi JunctionAgent tự đánh giá    │  │
│   │ Vòng 2: coalition — nút tắc nói chuyện với hàng xóm  │  │
│   │ Vòng 3: validate  — TẤT ĐỊNH, chặn ở đây             │  │
│   │ Vòng 4: approve   — SupervisorAgent giải xung đột    │  │
│   └──────────────────────────────────────────────────────┘  │
└──────────────────┬──────────────────────────────────────────┘
                   │ ghi mọi thứ
        ┌──────────▼───────────┐      ┌────────────────────┐
        │ PostgreSQL 17        │◄─────│ FastAPI + WebSocket│
        │ runs/messages/       │      │  → Web dashboard   │
        │ decisions/llm_calls/ │      └────────────────────┘
        │ metrics              │
        └──────────────────────┘
```

### 3.2 Quy tắc số 1: sim không bao giờ chờ LLM

```python
# sim/runner.py — nguyên tắc cốt lõi
async def run(self) -> None:
    pending: asyncio.Task | None = None
    while self.step < self.max_steps:
        self.conn.simulationStep()
        self.step += 1

        # tới chu kỳ quyết định → kích hoạt agent, KHÔNG await
        if self.step % DECISION_INTERVAL == 0:
            if pending is None:
                snapshot = self.collect_state()          # rẻ, tất định
                pending = asyncio.create_task(self.orchestrator.decide(snapshot))
            else:
                await self.store.log_skipped_cycle(self.step)   # chu kỳ trước chưa xong

        # kết quả về lúc nào áp lúc đó (trễ vài giây mô phỏng — chấp nhận được)
        if pending is not None and pending.done():
            self.apply(pending.result())
            pending = None

        await asyncio.sleep(0)   # nhường event loop
```

Chu kỳ LLM chưa xong mà đã tới chu kỳ sau → **bỏ qua**, ghi `skipped_cycle`. Đừng xếp hàng: quyết định dựa trên trạng thái cũ còn tệ hơn không quyết định. `skipped_cycle` là một metric trên dashboard.

### 3.3 Call site LLM duy nhất

```python
# agents/llm.py — TOÀN BỘ lời gọi model đi qua đây
from openai import AsyncOpenAI
from pydantic import BaseModel

client = AsyncOpenAI()          # đọc OPENAI_API_KEY từ env

MODELS = {                       # nằm trong config, không hardcode rải rác
    "junction":   ("gpt-5.6-luna",  "low"),
    "supervisor": ("gpt-5.6-terra", "medium"),
    "scenario":   ("gpt-5.6-sol",   "high"),
}

async def ask[T: BaseModel](
    role: str, system: str, user: str, schema: type[T], *, cache_key: str
) -> tuple[T, Usage]:
    model, effort = MODELS[role]
    resp = await client.responses.parse(
        model=model,
        input=[                                  # ổn định trước, biến thiên sau
            {"role": "system", "content": system},   # bất biến suốt run → cache
            {"role": "user",   "content": user},     # trạng thái của chu kỳ này
        ],
        text_format=schema,                      # → resp.output_parsed đã validate
        reasoning={"effort": effort},
        prompt_cache_key=cache_key,              # vd "junction:J12:v1" — ổn định!
    )
    return resp.output_parsed, extract_usage(resp)
```

Ba chi tiết đáng tiền:
- `system` phải **bất biến từng byte** suốt cả run (vai trò, hình học nút, ràng buộc). Mọi thứ thay đổi theo chu kỳ đi vào `user`. Đây là điều kiện để ăn cache 90%.
- `prompt_cache_key` ổn định theo nút (`f"junction:{jid}:v1"`), đổi `v1`→`v2` khi bạn sửa system prompt.
- `text_format=schema` loại bỏ trọn vẹn lớp lỗi parse JSON thủ công.

> Ghi chú kiểm chứng: `responses.parse` / `text_format` / `output_parsed` đã xác nhận từ tài liệu OpenAI. Đường dẫn chính xác của field usage cache (`usage.input_tokens_details.cached_tokens`) cần đối chiếu lại với SDK ở Bước 9 — in nguyên `resp.usage` ra một lần rồi cố định.

---

## 4. Sinh mạng lưới & kịch bản

```
networks/
├── grid_4x4/        # netgenerate --grid   → chuẩn, dùng so sánh baseline
├── mixed_district/  # netgenerate --rand + chỉnh tay → "đa dạng"
└── osm_real/        # osmWebWizard → 1 quận thật, để demo
```

**Vai trò của AI: sinh spec, không sinh XML.**

```python
# scenario/spec.py
class ScenarioSpec(BaseModel):
    name: str
    topology: Literal["grid", "spider", "random", "osm"]
    grid_size: tuple[int, int] | None = None
    junction_types: list[JunctionSpec]
    od_matrix: list[ODFlow]        # from_edge, to_edge, veh_per_hour, begin, end
    incidents: list[IncidentSpec]  # type, edge, begin, duration, severity
    seed: int
```

Luồng: mô tả tiếng Việt → `gpt-5.6-sol` sinh `ScenarioSpec` (structured output) → `scenario/build.py` dịch thành lệnh `netgenerate` + `randomTrips.py` + `.add.xml`. Spec được Pydantic validate **trước khi** chạy SUMO — model sai thì fail sớm và rõ ràng, không đẻ ra XML hỏng.

---

## 5. Lớp an toàn (làm TRƯỚC khi làm agent)

`safety/validator.py` — thuần Python, không LLM, có unit test riêng:

```python
HARD_CONSTRAINTS = {
    "min_green_s":        7,     # không bao giờ được vi phạm
    "max_green_s":        90,
    "yellow_s":           3,     # CỐ ĐỊNH — agent không được đụng
    "all_red_s":          2,     # CỐ ĐỊNH
    "min_cycle_s":        40,
    "max_cycle_s":        150,
    "max_delta_per_cycle_s": 15, # chống dao động
    "max_starvation_s":   120,   # mọi hướng phải được xanh trong khoảng này
}
```

Không gian hành động của `JunctionAgent` — **cố ý hẹp**:

| Action | Tham số | Ràng buộc |
|---|---|---|
| `adjust_phase_split` | `phase_id`, `delta_s` | `abs(delta) <= 15`, kết quả trong [min_green, max_green] |
| `set_cycle_length` | `cycle_s` | trong [40, 150] |
| `set_offset` | `offset_s` | để tạo làn sóng xanh với nút hàng xóm |
| `request_vms` | `edge`, `alt_route`, `duration_s` | tuyến thay thế phải tồn tại, không tạo vòng lặp |
| `no_action` | — | **phải hợp lệ và được khuyến khích** |

`no_action` quan trọng hơn vẻ ngoài: không có nó, model sẽ luôn "làm gì đó" và gây dao động hệ thống.

Chính sách: **clamp nếu chỉ vượt biên, reject nếu sai cấu trúc.** Mọi lần clamp/reject đều ghi log và hiện lên dashboard — đó cũng là một chỉ số chất lượng agent.

---

## 6. Observability

### 6.1 Schema PostgreSQL

```sql
CREATE TABLE runs (
  run_id UUID PRIMARY KEY,
  scenario TEXT NOT NULL, seed INTEGER NOT NULL,
  mode TEXT NOT NULL,                    -- 'llm' | 'fixed' | 'actuated' | 'maxpressure'
  started_at TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ,
  git_sha TEXT, config JSONB NOT NULL
);

CREATE TABLE messages (                  -- hội thoại giữa các agent
  id BIGSERIAL PRIMARY KEY,
  run_id UUID REFERENCES runs ON DELETE CASCADE,
  sim_time REAL NOT NULL, cycle_id INTEGER NOT NULL, round SMALLINT NOT NULL,
  sender TEXT NOT NULL, recipients TEXT[] NOT NULL,
  intent TEXT NOT NULL,                  -- report|request_help|propose|ack|object
  payload JSONB NOT NULL,                -- phần máy đọc
  rationale TEXT                         -- phần người đọc → hiển thị UI
);
CREATE INDEX ON messages (run_id, cycle_id);

CREATE TABLE decisions (
  id BIGSERIAL PRIMARY KEY,
  run_id UUID REFERENCES runs ON DELETE CASCADE,
  sim_time REAL NOT NULL, cycle_id INTEGER NOT NULL, junction_id TEXT NOT NULL,
  action_type TEXT NOT NULL, params JSONB NOT NULL,
  validator_status TEXT NOT NULL,        -- ok | clamped | rejected
  validator_violations JSONB,
  supervisor_verdict TEXT,               -- approved | modified | denied
  supervisor_reason TEXT,
  applied BOOLEAN NOT NULL DEFAULT false,
  effect JSONB                           -- metric trước/sau, điền ở chu kỳ kế
);
CREATE INDEX ON decisions (run_id, junction_id);

CREATE TABLE llm_calls (                 -- monitoring
  id BIGSERIAL PRIMARY KEY,
  run_id UUID REFERENCES runs ON DELETE CASCADE,
  sim_time REAL, agent_id TEXT NOT NULL, role TEXT NOT NULL,
  model TEXT NOT NULL, effort TEXT,
  input_tokens INT, output_tokens INT, reasoning_tokens INT, cached_tokens INT,
  latency_ms INT, status TEXT, error TEXT,
  cost_usd NUMERIC(10,6)
);
CREATE INDEX ON llm_calls (run_id, role);

CREATE TABLE metrics (                   -- lấy mẫu mỗi 10s mô phỏng
  run_id UUID REFERENCES runs ON DELETE CASCADE,
  sim_time REAL NOT NULL, junction_id TEXT NOT NULL,
  mean_waiting_s REAL, queue_len INT, throughput INT, mean_speed REAL, co2_mg REAL,
  PRIMARY KEY (run_id, sim_time, junction_id)
);
```

`cost_usd` tính ngay lúc gọi từ bảng giá §1.2 (nhớ `cached_tokens` tính giá cached) để dashboard không phải tính lại.

### 6.2 Dashboard (FastAPI + WebSocket) — 4 panel

1. **Live map** — Phase 1: `sumo-gui`; Phase 3: canvas tự vẽ từ vị trí xe stream qua WS
2. **Agent chat** — timeline hội thoại nhóm theo `cycle_id`, hiện `rationale`, bấm để xem `payload`
3. **Decision log** — tô màu theo `validator_status`/`supervisor_verdict`, kèm delta metric trước-sau
4. **Monitoring** — token & chi phí tích luỹ, **cache hit rate**, latency p50/p95, tỉ lệ reject, số `skipped_cycle`

### 6.3 Chế độ replay
Ghi toàn bộ I/O của LLM. Cờ `--replay <run_id>` chạy lại y hệt từ cache, **không gọi API**: demo tốn $0, không phụ thuộc mạng, debug UI không đốt token.

---

## 7. Baseline & metric (điều kiện để POC có kết luận)

Chạy **cùng scenario, cùng seed, cùng lịch sự cố** qua 4 chế độ: `fixed` (sàn), `actuated` (`<tlLogic type="actuated">` — **đối thủ thật sự**), `maxpressure` (~80 dòng Python, SOTA không học máy), `llm`.

Metric giao thông: mean waiting time, mean travel time, throughput, queue length p95, CO₂.
Metric hệ agent: chi phí/run, latency p50/p95, tỉ lệ reject, tỉ lệ `no_action`, `skipped_cycle`, cache hit rate.

> **Dự báo thẳng thắn**: nhiều khả năng `llm` sẽ **thua** `actuated` và `maxpressure` về waiting time thuần tuý. Điều đó **không** làm POC thất bại. Giá trị của hướng LLM nằm ở: xử lý tình huống bất thường chưa lập trình trước, phối hợp liên nút *có giải thích được*, và can thiệp đa phương thức (đèn + VMS cùng lúc). Thiết kế kịch bản demo làm nổi bật đúng những điểm đó, và **báo cáo trung thực cả chỗ thua**.

---

## 8. Cấu trúc repo

```
sumo-ai-agents/
├── docker/{Dockerfile.sim,Dockerfile.app,docker-compose.yml}
├── networks/                   # .net.xml, .rou.xml, .add.xml, .sumocfg (sinh ra)
├── scenarios/                  # *.yaml — spec kịch bản (commit vào git)
├── migrations/                 # Alembic
├── src/sumo_agents/
│   ├── sim/{runner,state,actuators,incidents}.py
│   ├── agents/{llm,junction,supervisor,protocol,orchestrator}.py
│   ├── safety/validator.py     # CÓ UNIT TEST
│   ├── baselines/{fixed,actuated,maxpressure}.py
│   ├── obs/{models,store,cost,replay}.py      # SQLAlchemy 2.x
│   ├── scenario/{spec,generate,build}.py
│   └── web/{app.py,static/}
├── tests/                      # chạy trên SQLite in-memory
├── mcp/server.py               # Phase 4
├── requirements.in / .txt      # pip-tools: khai báo / lock (COMMIT cả hai)
├── requirements-dev.in / .txt
├── pyproject.toml              # requires-python = ">=3.14", ruff, pytest
└── .env.example                # chỉ TÊN biến, không có giá trị
```

---

## 9. Roadmap

| Phase | Nội dung | Tiêu chí hoàn thành | Ước lượng |
|---|---|---|---|
| **0. Nền** | `.venv` + Python 3.14, `pip install eclipse-sumo`, grid 4x4, TraCI đổi đèn, Postgres + Alembic, ghi metric | Chạy 1h mô phỏng headless, có biểu đồ waiting time từ DB | 2–3 ngày |
| **1. Baseline + Safety** | 3 baseline + validator + test | Bảng so sánh 3 chế độ trên cùng seed | 2–3 ngày |
| **2. Agent lõi** | `llm.py`, JunctionAgent, Supervisor, orchestrator, log đầy đủ | `llm` chạy hết 1h, mọi message/decision/llm_call có trong DB | 4–6 ngày |
| **3. Dashboard** | 4 panel + WebSocket + replay | Demo được cho người ngoài mà không cần giải thích code | 3–4 ngày |
| **4. Mở rộng** | Sinh scenario bằng AI, OSM, MCP server | Sinh 1 scenario mới từ prompt tiếng Việt | 3–5 ngày |

Tổng **~3–4 tuần** toàn thời gian. Phase 0–2 là POC tối thiểu có thể kết luận.

---

## 10. Docker & môi trường

```yaml
# docker/docker-compose.yml (rút gọn)
services:
  db:
    image: postgres:17-alpine
    environment: {POSTGRES_DB: sumo, POSTGRES_PASSWORD: ${DB_PASSWORD:?required}}
    volumes: ["pgdata:/var/lib/postgresql/data"]
    healthcheck: {test: ["CMD-SHELL", "pg_isready -U postgres"], interval: 5s, retries: 10}
  sim:
    build: {context: .., dockerfile: docker/Dockerfile.sim}   # python:3.14-slim + pip install -r requirements.txt
    environment: [OPENAI_API_KEY, "DATABASE_URL=postgresql+psycopg://postgres:${DB_PASSWORD}@db/sumo"]
    depends_on: {db: {condition: service_healthy}}
    volumes: ["../networks:/app/networks"]
  app:
    build: {context: .., dockerfile: docker/Dockerfile.app}
    ports: ["8000:8000"]
    depends_on: {db: {condition: service_healthy}}
volumes: {pgdata: {}}
```

Lưu ý môi trường (bạn chạy **WSL2 / Ubuntu 24.04**):
- Base image `python:3.14-slim` + `apt-get install -y --no-install-recommends libatomic1` + `pip install -r requirements.txt` (đã gồm `eclipse-sumo`) — **không cần PPA**. `libatomic1` là bắt buộc, xem §1.1.
- Trong container **không cần `.venv`** (đã cô lập sẵn); `.venv` chỉ dùng cho dev trên WSL2. Cùng một `requirements.txt` cho cả hai → môi trường khớp nhau.
- `sumo-gui` trong Docker cần X11/VNC — phiền. Container chạy **headless**; cần GUI thì chạy native trên WSL2 (WSLg hỗ trợ sẵn).
- `libsumo` nhanh hơn `traci` ~3–10× nhưng **không hỗ trợ GUI** và chỉ 1 sim/process. Viết một lớp bọc mỏng để đổi bằng cờ config.

---

## 11. Chi phí ước tính (đã cập nhật theo giá OpenAI)

6 agent × 40 chu kỳ × 2 vòng = 480 lượt junction + 40 lượt supervisor, có prompt caching (~70% hit):

```
JunctionAgent  (gpt-5.6-luna, effort=low)
  input   1.2M tok → 0.84M cached @$0.02 + 0.36M @$0.20  ≈ $0.09
  output  ~576K tok (gồm reasoning) @$1.20               ≈ $0.69
                                                    ─────────────
                                                          ≈ $0.78

SupervisorAgent (gpt-5.6-terra, effort=medium)
  input   160K tok → 112K cached @$0.20 + 48K @$2.00     ≈ $0.12
  output  ~80K tok (gồm reasoning) @$12.00               ≈ $0.96
                                                    ─────────────
                                                          ≈ $1.08
═══════════════════════════════════════════════════════════════
TỔNG                                        ≈ $1.9 / run 1 giờ
```

Rẻ hơn ~4× so với ước tính v0.1 dùng Opus 5. Con số nhạy nhất là **reasoning tokens** (tính vào output) — đo thật ở run đầu rồi hiệu chỉnh. Ba cách giảm theo thứ tự nên thử:
1. **Prompt caching** — kiểm tra `cached_tokens > 0`, đây là khoản lớn nhất và miễn phí về chất lượng
2. **Coalition gating** — nút thông thoáng không gọi LLM (cắt được 50–70%)
3. Hạ `effort` xuống `none`/`low` cho junction

`--replay` đưa chi phí demo về **$0**.

---

## 12. Cảnh báo cần biết trước khi bắt đầu

### 12.1 Dữ liệu gửi ra ngoài
Mỗi chu kỳ quyết định **gửi trạng thái mô phỏng tới OpenAI API** (dịch vụ bên ngoài). Ở POC dữ liệu hoàn toàn tổng hợp nên không vấn đề. **Nếu sau này nạp dữ liệu đếm xe thật, camera, hay biển số** thì phải rà lại: đó có thể là thông tin cá nhân/nhạy cảm và không được gửi đi khi chưa có phê duyệt.

### 12.2 Giấy phép
| Thành phần | Giấy phép | Ghi chú |
|---|---|---|
| **Eclipse SUMO** (`eclipse-sumo`, `traci`, `sumolib`, `libsumo`) | **EPL-2.0** | ⚠️ Copyleft yếu, phạm vi file. Dùng như tiến trình/thư viện riêng thường ổn; **thương mại hoá thì cho pháp chế rà trước** |
| `openai`, FastAPI, Pydantic, SQLAlchemy, Alembic | MIT | An toàn |
| Uvicorn | BSD-3-Clause | An toàn |
| PostgreSQL | PostgreSQL License (kiểu BSD) | An toàn |
| psycopg 3 | LGPL-3.0 | ⚠️ Dùng như thư viện thì ổn; chú ý nếu link tĩnh/phân phối lại |
| Dữ liệu OpenStreetMap | **ODbL** | ⚠️ Share-alike với *dữ liệu* dẫn xuất |
| Langfuse (nếu dùng tracing) | Core MIT + phần enterprise thương mại | Kiểm tra phần bạn dùng |

Tự xác nhận lại trước khi đưa vào sản phẩm — có thể thay đổi theo phiên bản.

### 12.3 Secrets
`OPENAI_API_KEY` và `DB_PASSWORD` chỉ đọc từ biến môi trường / `.env` (đã trong `.gitignore`). Không hardcode, không log ra dashboard, không đưa vào prompt. `.env.example` chỉ chứa **tên** biến.

### 12.4 Prompt injection
Nội dung từ bản đồ OSM (tên đường, tag) và scenario spec do model sinh ra là **dữ liệu, không phải mệnh lệnh**. Khi đưa vào prompt agent, bọc trong delimiter rõ ràng và nêu trong system prompt rằng nội dung bên trong không được coi là chỉ thị. Cài sẵn ở `agents/protocol.py` ngay từ đầu.

---

## 13. Tóm tắt quyết định v0.2

| Quyết định | Kết luận | Ghi chú |
|---|---|---|
| Python 3.14 | ✅ Nhận | Không blocker; `libsumo` có wheel cp314; cài SUMO bằng pip, bỏ PPA |
| OpenAI | ✅ Nhận | Responses API; luna/terra/sol theo vai trò; rẻ hơn ~4× |
| PostgreSQL | ✅ Nhận | Lý do là concurrency, không phải dung lượng. Test vẫn dùng SQLite. Không TimescaleDB |
| LangChain | ❌ Không | Che mất payload → phá cache 90%. asyncio thuần ~250 dòng |
| LangGraph | ⏸️ Chưa | Chuyển khi routing động / cần checkpoint / cần human-in-the-loop (Phase 4+) |

Các bước triển khai chi tiết: xem [STEPS.md](STEPS.md).
