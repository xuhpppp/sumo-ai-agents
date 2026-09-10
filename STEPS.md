# STEPS — Các bước triển khai

> Đi kèm [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) · v0.2 · 2026-09-09
> Môi trường đã xác nhận: Ubuntu 24.04 (WSL2) · `/usr/bin/python3.14` có sẵn · Docker có sẵn · mạng OK

**Cách dùng**: mỗi bước có **DoD** (Definition of Done) — điều kiện nghiệm thu. Không sang bước sau khi DoD chưa đạt. Mỗi bước là một commit. Trạng thái: ⬜ chưa làm · 🔄 đang làm · ✅ đã đạt DoD.

**Nguyên tắc xuyên suốt**: những bước có dấu 🔒 là *điều kiện tiên quyết về an toàn/đo lường* — chúng phải xong trước khi gọi LLM lần đầu tiên. Đây là lý do Bước 1–9 không có dòng code AI nào.

---

## Bảng tiến độ

| # | Bước | Phase | Ước lượng | Trạng thái |
|---|---|---|---|---|
| 1 | Khởi tạo repo + `.venv` + Python 3.14 | 0 | 45' | ✅ |
| 2 | Cài SUMO qua pip, chạy thử | 0 | 30' | ✅ |
| 3 | Sinh mạng lưới `grid_4x4` | 0 | 1h | ✅ |
| 4 | Postgres + SQLAlchemy + Alembic | 0 | 2h | ✅ |
| 5 | `SimRunner` vòng lặp trần + thu metric | 0 | 3h | ⬜ |
| 6 | Biểu đồ waiting time từ DB | 0 | 1h | ⬜ |
| 7 | 🔒 `validator.py` + unit test | 1 | 3h | ⬜ |
| 8 | 3 baseline: fixed / actuated / maxpressure | 1 | 4h | ⬜ |
| 9 | 🔒 Bảng so sánh baseline (harness chạy nhiều run) | 1 | 2h | ⬜ |
| 10 | `agents/llm.py` — call site duy nhất | 2 | 2h | ⬜ |
| 11 | `protocol.py` — schema Pydantic | 2 | 2h | ⬜ |
| 12 | `JunctionAgent` (vòng 1: observe) | 2 | 4h | ⬜ |
| 13 | Coalition + `SupervisorAgent` (vòng 2–4) | 2 | 5h | ⬜ |
| 14 | Nối vào SimRunner, chạy full 1h | 2 | 3h | ⬜ |
| 15 | Backend dashboard + WebSocket | 3 | 3h | ⬜ |
| 16 | 4 panel frontend | 3 | 5h | ⬜ |
| 17 | Chế độ `--replay` | 3 | 2h | ⬜ |
| 18 | Mạng lưới đa dạng (mixed + OSM) | 3 | 3h | ⬜ |
| 19 | VMS / rerouting + compliance rate | 4 | 3h | ⬜ |
| 20 | Sinh scenario bằng AI | 4 | 3h | ⬜ |
| 21 | MCP server | 4 | 4h | ⬜ |

---

# PHASE 0 — Nền móng

## Bước 1 · Khởi tạo repo + `.venv` + Python 3.14

**Mục tiêu**: môi trường tái lập được, khoá phiên bản.

> Đã kiểm chứng trên máy này: `python3.14 -m venv` chạy tốt (Python 3.14.6, pip 26.1.2, gói `python3.14-venv` đã cài sẵn). Không cần cài thêm gì.

```bash
cd /home/phucdt/personal_code/sumo-ai-agents
git init

python3.14 -m venv .venv
source .venv/bin/activate            # LÀM LẠI mỗi khi mở shell mới
python -m pip install --upgrade pip pip-tools
```

Khai báo dependency ở `requirements.in` (chỉ ghi thứ bạn thực sự dùng, không ghi phiên bản):

```
pydantic
sqlalchemy
alembic
psycopg[binary]
fastapi
uvicorn[standard]
openai
typer
rich
```

`requirements-dev.in`:
```
-c requirements.txt
pytest
pytest-asyncio
ruff
mypy
```

Khoá phiên bản:
```bash
pip-compile requirements.in      -o requirements.txt
pip-compile requirements-dev.in  -o requirements-dev.txt
pip install -r requirements.txt -r requirements-dev.txt
```

**Vì sao dùng `pip-tools` chứ không chỉ `pip install`**: dự án này so sánh các run cách nhau nhiều tuần (baseline vs `llm`). Nếu phiên bản SUMO hay thư viện đổi giữa chừng mà không ai biết, kết quả so sánh mất giá trị. `requirements.txt` được pin đầy đủ và **commit vào git** chính là thứ giữ cho các run so sánh được với nhau. Nếu muốn nhẹ hơn nữa thì `pip freeze > requirements.txt` cũng được, chỉ là khó tách dependency trực tiếp với dependency gián tiếp.

Thêm `pyproject.toml` cho phần đóng gói mã nguồn (`requires-python = ">=3.14"`, `[tool.ruff]`, `[tool.pytest.ini_options]`) rồi `pip install -e .` để import được `sumo_agents` từ bất kỳ đâu.

Tạo `.gitignore` (bắt buộc có `.env`, `.venv/`, `data/`, `networks/*/`) và `.env.example` — **chỉ tên biến, không giá trị**:
```
OPENAI_API_KEY=
DB_PASSWORD=
DATABASE_URL=
```

**DoD**: `.venv` đã activate · `python -c "import sys; print(sys.version)"` in ra 3.14.x · `requirements.txt` đã pin và được commit · `git status` không thấy `.env` hay `.venv/`

---

## Bước 2 · Cài SUMO qua pip

**Mục tiêu**: có binary SUMO + Python binding, không dùng PPA.

Thêm vào `requirements.in`:
```
eclipse-sumo
traci
sumolib
libsumo
```

```bash
pip-compile requirements.in -o requirements.txt && pip install -r requirements.txt
python -c "import traci, sumolib, libsumo; print('ok')"
sumo --version                        # binary nằm trong .venv/bin
```

> Đã kiểm chứng: `eclipse-sumo` 1.27.1 ship sẵn binary trong wheel manylinux; `libsumo` có wheel `cp314`. Không cần PPA, không cần `apt install sumo`.

**⚠️ Một dependency hệ thống bắt buộc.** Wheel `eclipse-sumo` **không** đóng gói kèm `libatomic.so.1`, mà cả 14 binary (`sumo`, `sumo-gui`, `netgenerate`, `netconvert`, `duarouter`, ...) đều link tới nó. Thiếu nó thì mọi binary chết ngay khi khởi động:

```
sumo: error while loading shared libraries: libatomic.so.1:
      cannot open shared object file: No such file or directory
```

Ubuntu 24.04 **không** cài sẵn gói này. Sửa:

```bash
sudo apt install -y libatomic1
```

Đây là dependency hệ thống *duy nhất* còn thiếu — đã kiểm tra `ldd` trên toàn bộ 14 binary, mọi thư viện khác đều resolve được. Nhớ đưa nó vào `Dockerfile.sim` (Bước tạo Docker), vì `python:3.14-slim` cũng không có sẵn.

Cách tự kiểm tra khi gặp lỗi tương tự với binary khác:
```bash
ldd .venv/lib/python3.14/site-packages/sumo/bin/sumo | grep "not found"
```

Vì SUMO nằm trong `.venv`, **không cần đặt `SUMO_HOME` thủ công** — nhưng vài script trong `sumo-tools` vẫn đọc biến này. Thêm vào `.env`:
```bash
SUMO_HOME=$(python -c "import sumo, pathlib; print(pathlib.Path(sumo.__file__).parent)")
```

Tạo `src/sumo_agents/sim/conn.py` — lớp bọc mỏng để đổi `traci` ↔ `libsumo` bằng cờ config (libsumo nhanh hơn 3–10× nhưng không có GUI).

**DoD**: `sumo --version` in ra 1.27.x (không lỗi shared library) · import cả 3 module thành công · lớp bọc `conn.py` chạy được cả hai backend

---

## Bước 3 · Sinh mạng lưới `grid_4x4`

**Mục tiêu**: có mạng lưới + nhu cầu giao thông chạy được, tái lập bằng seed.

```bash
mkdir -p networks/grid_4x4

netgenerate --grid --grid.number=4 --grid.length=200 \
  --tls.guess true --default.lanenumber 2 \
  --output-file networks/grid_4x4/net.xml

python "$SUMO_HOME/tools/randomTrips.py" \
  -n networks/grid_4x4/net.xml -o networks/grid_4x4/trips.xml \
  -b 0 -e 3600 -p 0.8 --seed 42 --validate
```

Viết `networks/grid_4x4/sim.sumocfg`. Thêm `--device.rerouting.probability 1.0` để Bước 19 (VMS) hoạt động.

**DoD**: `sumo -c networks/grid_4x4/sim.sumocfg` chạy hết 3600 step không lỗi · chạy 2 lần cùng seed cho kết quả giống hệt

---

## Bước 4 · Postgres + SQLAlchemy + Alembic

**Mục tiêu**: tầng lưu trữ sẵn sàng trước khi có dữ liệu để lưu.

```bash
# docker/docker-compose.yml — chỉ service db ở bước này
docker compose -f docker/docker-compose.yml up -d db
```

- `obs/models.py`: khai báo 5 bảng của §6.1 plan bằng SQLAlchemy 2.x (typed, `Mapped[...]`)
- `alembic init migrations` → `alembic revision --autogenerate -m "init"` → `upgrade head`
- `obs/store.py`: lớp ghi async, gom batch cho bảng `metrics` (đừng commit từng dòng)
- `tests/conftest.py`: fixture SQLite in-memory để test không cần container

**DoD**: `alembic upgrade head` tạo đủ 5 bảng · `pytest` chạy được trên SQLite không cần Docker

---

## Bước 5 · `SimRunner` vòng lặp trần + thu metric

**Mục tiêu**: chạy được 1 giờ mô phỏng và ghi metric — **chưa có AI**.

- `sim/runner.py`: vòng lặp async theo §3.2 plan, nhưng `orchestrator=None` (chưa có agent)
- `sim/state.py`: `collect_state()` → queue length, waiting time, phase hiện tại, flow từ nút hàng xóm. Phải **rẻ và tất định**
- Lấy mẫu metric mỗi 10s mô phỏng → bảng `metrics`
- `sim/incidents.py`: bơm sự cố theo **lịch có seed** từ YAML, không random runtime

**DoD**: `python -m sumo_agents.sim.runner --scenario grid_4x4 --mode fixed` chạy hết 3600s, bảng `metrics` có ~5.760 dòng · chạy 2 lần cùng seed → số liệu trùng khớp

---

## Bước 6 · Biểu đồ waiting time từ DB

**Mục tiêu**: nhìn thấy được dữ liệu, kiểm chứng sự cố có thật sự gây tắc.

Script `scripts/plot_run.py` — query `metrics`, vẽ mean waiting time theo thời gian, đánh dấu thời điểm sự cố.

**DoD**: có file PNG cho thấy waiting time **tăng rõ rệt** đúng lúc sự cố xảy ra. Nếu không thấy khác biệt → nhu cầu giao thông quá thấp, chỉnh `-p` ở Bước 3 rồi làm lại.

> 🚩 Đây là checkpoint quan trọng: nếu sự cố không tạo được tắc đường, thì không có gì cho agent giải quyết và toàn bộ POC vô nghĩa. Đừng đi tiếp khi chưa đạt.

---

# PHASE 1 — Baseline + An toàn

## Bước 7 · 🔒 `validator.py` + unit test

**Mục tiêu**: lớp an toàn tất định, xong **trước** khi có LLM.

- `safety/validator.py`: `HARD_CONSTRAINTS` + hàm `validate(action, tls_state) -> ValidationResult(ok, violations, clamped_action)`
- Chính sách: **clamp nếu vượt biên, reject nếu sai cấu trúc**
- `tests/test_validator.py` — bao phủ: min/max green, cấm sửa yellow/all-red, giới hạn cycle, `max_delta_per_cycle`, chống bỏ đói hướng, và **`no_action` luôn hợp lệ**

**DoD**: ≥15 test case pass · coverage `validator.py` ≥ 90% · không import gì liên quan LLM

---

## Bước 8 · 3 baseline

**Mục tiêu**: có đối thủ để so sánh.

- `baselines/fixed.py` — chu kỳ cố định (điểm sàn)
- `baselines/actuated.py` — dùng `<tlLogic type="actuated">` sẵn có của SUMO (**đối thủ thật sự**)
- `baselines/maxpressure.py` — Max-Pressure, ~80 dòng, SOTA không học máy

Cả ba cắm vào cùng interface `Controller.decide(snapshot) -> list[Action]` mà `llm` sẽ dùng ở Phase 2.

**DoD**: cả 3 chạy hết 3600s · `maxpressure` tốt hơn `fixed` về mean waiting time (nếu không, nó đang sai)

---

## Bước 9 · 🔒 Bảng so sánh baseline

**Mục tiêu**: harness chạy nhiều run và ra bảng — hạ tầng đo lường hoàn chỉnh.

`scripts/compare.py`: chạy N chế độ × M seed, ghi vào `runs`, xuất bảng markdown (mean waiting, travel time, throughput, queue p95, CO₂) kèm khoảng tin cậy.

**DoD**: một lệnh sinh ra bảng so sánh 3 chế độ × 3 seed. Bảng này chính là **bảng kết quả cuối cùng của POC** — chỉ thêm cột `llm` ở Bước 14.

> 🚩 Hoàn thành Bước 9 nghĩa là bạn đã có một POC hoàn chỉnh *không cần AI*. Mọi thứ sau đây là thêm lớp AI vào một nền đã đo được.

---

# PHASE 2 — Agent lõi

## Bước 10 · `agents/llm.py` — call site duy nhất

**Mục tiêu**: mọi lời gọi model đi qua đúng một hàm.

- Theo §3.3 plan: `responses.parse` + `text_format` + `reasoning.effort` + `prompt_cache_key`
- `MODELS` dict trong config: junction=`gpt-5.6-luna`/low · supervisor=`gpt-5.6-terra`/medium · scenario=`gpt-5.6-sol`/high
- Ghi `llm_calls` **mọi lượt gọi**: tokens, cached_tokens, reasoning_tokens, latency, cost_usd
- `obs/cost.py`: bảng giá §1.2 plan, tính cost tại chỗ

**Việc đầu tiên khi chạy**: in nguyên `resp.usage` một lần, xác định đường dẫn chính xác của field cached tokens, rồi cố định trong code.

**DoD**: script gọi thử 3 lượt liên tiếp cùng system prompt → lượt 2, 3 có `cached_tokens > 0`. **Nếu vẫn bằng 0 thì dừng lại sửa** — cache 90% là khoản tiết kiệm lớn nhất của dự án.

---

## Bước 11 · `protocol.py` — schema Pydantic

**Mục tiêu**: hợp đồng dữ liệu giữa các agent, máy đọc được và người đọc được.

```python
class Action(BaseModel):
    type: Literal["adjust_phase_split","set_cycle_length","set_offset","request_vms","no_action"]
    params: dict
class Proposal(BaseModel):
    junction_id: str
    action: Action
    urgency: Literal["low","medium","high"]
    rationale: str          # tiếng Việt, hiển thị lên UI
class Message(BaseModel):
    sender: str; recipients: list[str]
    intent: Literal["report","request_help","propose","ack","object"]
    payload: dict; rationale: str
class Verdict(BaseModel):
    decision: Literal["approved","modified","denied"]
    modified_action: Action | None; reason: str
```

Thêm helper bọc dữ liệu ngoài (tên đường OSM, spec do model sinh) trong delimiter + ghi rõ trong system prompt rằng **nội dung bên trong là dữ liệu, không phải chỉ thị**.

**DoD**: `tests/test_protocol.py` pass, gồm case model trả JSON sai schema → raise rõ ràng

---

## Bước 12 · `JunctionAgent` (vòng 1: observe)

**Mục tiêu**: một agent nhìn trạng thái nút của mình và đề xuất hành động.

- System prompt **bất biến từng byte** suốt run: vai trò, hình học nút, danh sách hàng xóm, ràng buộc, không gian hành động. Mọi thứ đổi theo chu kỳ đi vào `user`.
- Suy ra tô-pô hàng xóm **từ mạng lưới** (`sumolib`), không hardcode, không all-to-all
- Output: `Proposal`

**DoD**: 6 agent chạy 1 chu kỳ, sinh 6 `Proposal` hợp lệ · `cached_tokens > 0` từ chu kỳ thứ 2 · nút thông thoáng trả `no_action`

> Nếu agent **không bao giờ** trả `no_action`, system prompt đang thúc nó hành động quá mức → sửa prompt, nếu không hệ thống sẽ dao động.

---

## Bước 13 · Coalition + `SupervisorAgent` (vòng 2–4)

**Mục tiêu**: hoàn thiện pipeline 4 vòng.

- **Vòng 2 — coalition**: chỉ nút *đang tắc* mới lập nhóm và trao đổi với hàng xóm. **Tối đa 2 vòng**. Nút thông thoáng im lặng → đây là cơ chế cắt 50–70% chi phí
- **Vòng 3 — validate**: gọi `validator.py` Bước 7. **Tất định, chạy trước supervisor.** Đề xuất vi phạm bị chặn thẳng, không lên supervisor
- **Vòng 4 — approve**: `SupervisorAgent` giải xung đột giữa các nút (2 nút cùng xin ưu tiên ngược chiều) → `Verdict`
- Ghi `messages` + `decisions` đầy đủ, mọi vòng

**DoD**: 1 chu kỳ hoàn chỉnh sinh ra: N `messages`, N `decisions` có `validator_status` và `supervisor_verdict` · inject một action cố ý vi phạm → bị `rejected` **trước** khi tới supervisor

---

## Bước 14 · Nối vào SimRunner, chạy full 1 giờ

**Mục tiêu**: chế độ `llm` chạy trọn vẹn.

- Cắm `orchestrator` vào `SimRunner` theo §3.2 — **async, sim không chờ**
- Xử lý `skipped_cycle` khi chu kỳ trước chưa xong
- Điền `decisions.effect` ở chu kỳ kế (metric trước/sau)

**DoD**: chạy hết 3600s không crash · thêm cột `llm` vào bảng so sánh Bước 9 · chi phí thực tế/run được ghi lại và đối chiếu với ước tính ~$2

> Nhắc lại từ plan §7: `llm` **có thể thua** `actuated`. Đó không phải thất bại — ghi nhận trung thực rồi chuyển sang thiết kế kịch bản làm nổi bật thứ mà baseline không làm được (sự cố bất thường, phối hợp liên nút có giải thích).

---

# PHASE 3 — Trực quan hoá

## Bước 15 · Backend dashboard + WebSocket
`web/app.py` FastAPI: REST cho lịch sử (`/runs`, `/runs/{id}/messages|decisions|metrics`), WebSocket đẩy sự kiện live.
**DoD**: `wscat` nhận được sự kiện realtime khi sim đang chạy

## Bước 16 · 4 panel frontend
Live map (Phase 3 tạm nhúng `sumo-gui`) · Agent chat (timeline theo `cycle_id`, hiện `rationale`) · Decision log (tô màu theo status) · Monitoring (token, chi phí, **cache hit rate**, latency p50/p95, reject rate, skipped_cycle).
**DoD**: người ngoài xem dashboard hiểu được chuyện gì đang xảy ra mà không cần giải thích code

## Bước 17 · Chế độ `--replay`
Cache toàn bộ I/O LLM theo `run_id`; `--replay` chạy lại không gọi API.
**DoD**: replay một run cũ, chi phí **$0**, dashboard hiển thị y hệt lần chạy gốc

## Bước 18 · Mạng lưới đa dạng
`mixed_district` (netgenerate --rand + chỉnh tay: ngã ba T, vòng xuyến, nút lệch) và `osm_real` (osmWebWizard, 1 quận thật).
**DoD**: cả 3 mạng chạy được cả 4 chế độ · ⚠️ dữ liệu OSM là **ODbL**, xem plan §12.2

---

# PHASE 4 — Mở rộng

## Bước 19 · VMS / rerouting + compliance rate
`traci.vehicle.setAdaptedTraveltime()`, chỉ xe có `device.rerouting` phản ứng. Mô hình **compliance rate** (mặc định 0.4) — không phải ai cũng nghe theo biển.
**DoD**: một `request_vms` làm thay đổi phân bố lưu lượng có đo được trên tuyến thay thế

## Bước 20 · Sinh scenario bằng AI
Prompt tiếng Việt → `gpt-5.6-sol` sinh `ScenarioSpec` (structured output) → Pydantic validate → `build.py` dịch thành lệnh `netgenerate`/`randomTrips`. **AI không bao giờ sinh trực tiếp `.net.xml`.**
**DoD**: từ một câu mô tả tiếng Việt, sinh ra scenario chạy được end-to-end

## Bước 21 · MCP server
`mcp/server.py` expose: `query_sim_state`, `list_runs`, `compare_runs`, `run_scenario`.
**DoD**: hỏi chuyện được với sim đang chạy từ trong Claude Code

---

## Ghi chú vận hành

- **Mỗi bước một commit**, message ghi rõ số bước: `feat(step-7): deterministic safety validator`
- **Chưa gọi LLM trước Bước 10.** Bước 1–9 hoàn toàn tất định và miễn phí — đó là chủ ý
- **Hai checkpoint dừng-nếu-fail**: Bước 6 (sự cố phải gây tắc thật) và Bước 10 (cache phải hit)
- **Chi phí**: chỉ Bước 10–14 và 20 tốn tiền API. Ước tính toàn bộ giai đoạn phát triển < $50 nếu dùng `--replay` khi debug UI
- Secrets: xem plan §12.3. Không bao giờ log `OPENAI_API_KEY` ra dashboard hay đưa vào prompt

---

**Bắt đầu từ Bước 1.**
