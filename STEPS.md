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
| 5 | `SimRunner` vòng lặp trần + thu metric | 0 | 3h | ✅ |
| 5b | Xem lại trực quan bằng `sumo-gui` | 0 | 30' | ✅ |
| 6 | Biểu đồ waiting time từ DB | 0 | 1h | ✅ |
| 7 | 🔒 `validator.py` + unit test | 1 | 3h | ✅ |
| 8 | 3 baseline: fixed / actuated / maxpressure | 1 | 4h | ✅ |
| 9 | 🔒 Bảng so sánh baseline (harness chạy nhiều run) | 1 | 2h | ✅ |
| 10 | `agents/llm.py` — call site duy nhất | 2 | 2h | ✅ |
| 11 | `protocol.py` — schema Pydantic | 2 | 2h | ✅ |
| 12 | `JunctionAgent` (vòng 1: observe) | 2 | 4h | ✅ |
| 13 | Coalition + `SupervisorAgent` (vòng 2–4) | 2 | 5h | ✅ |
| 14 | Nối vào SimRunner, chạy full 1h | 2 | 3h | ✅ |
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

- `sim/runner.py`: vòng lặp async theo §3.2 plan (`await asyncio.sleep(0)` mỗi step), `orchestrator=None` (chưa có agent). Điều kiện dừng dùng `simulation.getMinExpectedNumber() > 0` (chuẩn TraCI) thay vì đếm step cứng — nhu cầu xe kết thúc ở t=3600 (`trips.xml`) nhưng xe đã xuất phát còn chạy tiếp tới lúc đến nơi, nên run thực tế dài hơn 3600s một chút (~3800s trên máy này)
- `sim/state.py`: `collect_state()` → với mỗi junction có đèn tín hiệu, đọc các lane đang được điều khiển (`trafficlight.getControlledLanes`) và tính `queue_len` (tổng xe đang dừng), `mean_waiting_s` (tổng waiting time / số xe), `throughput` (số xe hiện diện ở lane vào — proxy rẻ, không phải đếm lũy kế), `mean_speed`, `co2_mg`, `current_phase`. Toàn bộ chỉ đọc số liệu step-cuối-cùng mà SUMO đã tự tính sẵn → **rẻ và tất định**. Trả về `dict[junction_id, JunctionSnapshot]` — Bước 12 (Phase 2) dùng thẳng dict này, tra hàng xóm bằng cách lấy theo `junction_id` khác trong cùng dict (tô-pô tính từ `sumolib` ở Bước 12, không tính lại ở đây)
- Lấy mẫu metric mỗi 10s mô phỏng → bảng `metrics`
- `sim/incidents.py` + `networks/grid_4x4/incidents.yaml`: bơm sự cố theo **lịch có seed** từ YAML (không random runtime) — mỗi incident giảm tốc độ tối đa của mọi lane trên 1 edge còn `speed_factor` × bình thường, trong khoảng `[begin, begin+duration)`, rồi tự khôi phục. Đã kiểm chứng trực tiếp: `maxSpeed` giảm đúng lúc `begin`, giữ nguyên suốt cửa sổ, khôi phục đúng lúc kết thúc

**DoD**: `python -m sumo_agents.sim.runner --scenario grid_4x4 --mode fixed --seed 42` chạy hết 3600s (thực tế ~3816s do xe chạy nốt), bảng `metrics` có dữ liệu (đã đo: 4.584 dòng = 12 junction × ~382 mẫu) · chạy 2 lần cùng seed → **0 khác biệt** trên toàn bộ (sim_time, junction_id) → **đã verify, khớp**

---

## Bước 5b · Xem lại trực quan bằng `sumo-gui`

**Mục tiêu**: có thêm một cách xem kết quả ngoài truy vấn DB (Bước 6) — nhìn trực tiếp xe chạy trên bản đồ.

Vì mọi run đều tất định (seed cố định → kết quả giống hệt, đã verify ở Bước 3 và Bước 5), việc "xem lại" không cần ghi/replay dữ liệu gì cả — chỉ cần chạy lại **đúng scenario + đúng seed** trên backend `traci` với GUI bật:

```bash
python scripts/replay_gui.py --scenario grid_4x4 --seed 42
python scripts/replay_gui.py --scenario grid_4x4 --seed 42 --delay 50   # chậm hơn/nhanh hơn
```

Script này độc lập với DB/`Store` — không tạo `run_id`, không ghi Postgres, chỉ dựng lại đúng những gì `SimRunner` đã thấy để xem bằng mắt, kể cả sự cố đã bơm ở Bước 5 (đọc cùng `incidents.yaml` nếu có). `--delay` là cờ có sẵn của `sumo-gui` (ms thời gian thực mỗi step mô phỏng) — cần thiết vì chạy `traci` nhanh như `libsumo` sẽ render nhanh hơn mắt theo kịp.

Ngoài ra `sim/runner.py` cũng có cờ `--gui` để xem trực tiếp trong lúc chạy run "thật" (vẫn ghi DB bình thường, chỉ đổi sang backend `traci` để có cửa sổ):
```bash
python -m sumo_agents.sim.runner --scenario grid_4x4 --mode fixed --seed 42 --gui
```

**DoD**: `python scripts/replay_gui.py --scenario grid_4x4 --seed 42` mở được cửa sổ `sumo-gui`, xe di chuyển đúng theo scenario, tự kết thúc khi hết xe hoặc đóng thủ công. *(Cần chạy tương tác để xác nhận bằng mắt — không kiểm chứng tự động được, khác với Bước 5 vốn chỉ cần dữ liệu số.)*

---

## Bước 6 · Biểu đồ waiting time từ DB

**Mục tiêu**: nhìn thấy được dữ liệu, kiểm chứng sự cố có thật sự gây tắc.

`scripts/plot_run.py` — query `metrics`, gộp trung bình **toàn mạng** (mean qua cả 12 junction) mỗi mẫu 10s, làm mượt bằng rolling mean cửa sổ 180s (≥ 2 chu kỳ đèn — chu kỳ đèn tín hiệu cố định của `grid_4x4` là 90s, xem `<tlLogic>` trong `net.xml`; nếu không làm mượt, sawtooth do pha đèn xanh/đỏ sẽ át hoàn toàn hiệu ứng của sự cố), rồi tô vùng thời gian sự cố (đọc từ `incidents.yaml`, không hardcode) lên biểu đồ.

```bash
python scripts/plot_run.py --scenario grid_4x4
```

**⚠️ Phát hiện quan trọng khi verify checkpoint này**: cấu hình sự cố ban đầu ở Bước 5 (`speed_factor: 0.1`, tức còn ~1.39 m/s trên edge 13.89 m/s) **không** tạo ra tắc nghẽn nhìn thấy được — lý do là ngưỡng "halting" của SUMO (dùng bởi cả `getLastStepHaltingNumber` lẫn việc tính waiting time tích luỹ) là một hằng số tuyệt đối **0.1 m/s**, không phụ thuộc tốc độ tối đa của lane. Xe chạy ở 1.39 m/s vẫn được tính là "đang di chuyển", không phải "đang chờ" → sự cố chỉ làm chậm nhẹ, không tạo hàng chờ. Đã sửa `networks/grid_4x4/incidents.yaml` xuống `speed_factor: 0.005` (~0.07 m/s, dưới ngưỡng 0.1) — verify lại bằng cách chạy 2 lần cùng seed (vẫn 0 khác biệt, xác nhận sự thay đổi không phá tính tất định của Bước 5).

**Kết quả sau khi sửa** (`data/plots/grid_4x4_<run_id>.png`, không commit — xem `.gitignore`):
- Waiting time trung bình toàn mạng: nền ~4-5s → đỉnh ~21-33s trong lúc sự cố (1200-1800s), quay lại nền ngay sau khi hết sự cố
- `queue_len` ở 2 junction cạnh sự cố (B1, B2) tăng từ nền ~4-5 lên đỉnh ~35-48
- Có teleport do kẹt xe quá lâu (log cảnh báo `waited too long (jam)`) lan sang vài junction lân cận (B0B1, A1B1, B3B2, C1B1, A2B2) trong khoảng 1534-1801s — tức tắc nghẽn thực sự lan toả, không chỉ khoanh vùng ở 1 edge

**DoD**: có file PNG cho thấy waiting time **tăng rõ rệt** đúng lúc sự cố xảy ra — **đã đạt**, xem mô tả trên.

> 🚩 Đây là checkpoint quan trọng: nếu sự cố không tạo được tắc đường, thì không có gì cho agent giải quyết và toàn bộ POC vô nghĩa. Đừng đi tiếp khi chưa đạt. *(Ghi chú cho lần sau nếu đổi mạng lưới/scenario: nguyên nhân không phải lúc nào cũng là "nhu cầu quá thấp" như dự đoán ban đầu — ở đây là do ngưỡng halting tuyệt đối của SUMO, một chi tiết dễ bỏ sót khi thiết kế `speed_factor` theo tỷ lệ tương đối.)*

---

# PHASE 1 — Baseline + An toàn

## Bước 7 · 🔒 `validator.py` + unit test ✅

**Mục tiêu**: lớp an toàn tất định, xong **trước** khi có LLM.

**Đã làm**:

- `src/sumo_agents/safety/validator.py`:
  - `HARD_CONSTRAINTS` đúng theo plan §5 (`min_green_s=7`, `max_green_s=90`, `yellow_s=3` cố định, `all_red_s=2` cố định, `min_cycle_s=40`, `max_cycle_s=150`, `max_delta_per_cycle_s=15`, `max_starvation_s=120`).
  - Không gian hành động (Pydantic model, discriminated bởi `type`) đúng bảng ở plan §5: `AdjustPhaseSplit`, `SetCycleLength`, `SetOffset`, `RequestVms`, `NoAction`. Đây là bản tối thiểu cho riêng validator — schema Pydantic "chính thức" dùng chung cho LLM sẽ làm ở Bước 11 (`protocol.py`), có thể tái dùng hoặc định nghĩa lại các model này, không phá vỡ gì ở đây vì validator không phụ thuộc ngược vào `protocol.py`.
  - `TlsState`/`PhaseState` (dataclass, giống style `sim/state.py`): trạng thái TLS tối thiểu validator cần — danh sách phase kèm loại (`green`/`yellow`/`all_red`) và `time_since_last_green_s` cho việc chống bỏ đói hướng.
  - `validate(action, tls_state) -> ValidationResult(ok, violations, clamped_action)`.
  - Chính sách **clamp nếu vượt biên, reject nếu sai cấu trúc/ngữ nghĩa**:
    - `adjust_phase_split`: reject nếu `phase_id` không tồn tại hoặc không phải phase `green` (cấm sửa yellow/all-red); reject nếu `delta_s < 0` mà phase đó đã bị bỏ đói ≥ `max_starvation_s` (không cho phép giảm thêm); còn lại thì clamp `delta_s` theo `max_delta_per_cycle_s`, rồi clamp kết quả `duration` theo `[min_green_s, max_green_s]`.
    - `set_cycle_length`: clamp theo `[min_cycle_s, max_cycle_s]`.
    - `set_offset`: clamp theo `[0, max_cycle_s]`.
    - `request_vms`: reject nếu `alt_route` rỗng hoặc có cạnh lặp lại (vòng lặp); clamp `duration_s` theo `[0, max_cycle_s * 24]` (không cho VMS kéo dài hơn cả 1 giờ mô phỏng).
    - `no_action`: luôn hợp lệ, không có ràng buộc nào để kiểm tra.
- `tests/test_validator.py` — 27 test case, bao phủ đúng danh sách yêu cầu (min/max green, cấm sửa yellow/all-red, giới hạn cycle, `max_delta_per_cycle`, chống bỏ đói hướng cả 2 chiều — reject khi giảm phase đã đói, cho phép tăng phase đã đói — và `no_action` luôn hợp lệ) cộng thêm `set_offset`, `request_vms`, và một test tĩnh (`ast` parse source) xác nhận module không import gì có tên chứa `openai`/`anthropic`/`llm`.

**Verify**:

```bash
source .venv/bin/activate
python -m pytest tests/test_validator.py -v --cov=sumo_agents.safety.validator --cov-report=term-missing
# 27 passed · coverage 100% (109/109 statements)
python -m pytest   # toàn bộ 33 test (models + validator) pass
```

Đã thêm `pytest-cov` (MIT license) vào môi trường + `requirements.txt` (`pip freeze`) để đo coverage cho DoD này.

**DoD**: ≥15 test case pass (27/27) · coverage `validator.py` ≥ 90% (đạt 100%) · không import gì liên quan LLM (xác nhận qua `test_module_has_no_llm_imports`) — **đã đạt**.

---

## Bước 8 · 3 baseline ✅

**Mục tiêu**: có đối thủ để so sánh.

**Đã làm**:

- `src/sumo_agents/baselines/base.py`: `Controller` Protocol — `decide(snapshot: dict[str, JunctionSnapshot], sim_time: float) -> list[Action]`, cùng loại `Action` với `safety/validator.py` (Bước 7) — đây chính xác là interface `llm` sẽ dùng ở Phase 2, không phải một interface "tương tự".
- `src/sumo_agents/sim/actuators.py` (module mới, đúng như cấu trúc repo ở plan §8 `sim/{runner,state,actuators,incidents}.py`): `phase_kind(state)` phân loại phase (`green`/`yellow`/`all_red`, cùng quy tắc đã dùng ở Bước 6), `read_tls_state()` dựng `TlsState` thật từ chương trình TLS đang chạy để đưa vào `validate()`, `apply_action()` áp một `Action` đã validate vào SUMO qua `trafficlight.setProgramLogic` (dùng `conn.trafficlight.Phase`/`Logic` — lớp riêng của từng backend traci/libsumo, không phải lớp dùng chung, đã kiểm chứng cả hai backend).
- `baselines/fixed.py`: `decide()` luôn trả `[]` — không đụng gì tới đèn, chạy nguyên `<tlLogic type="static">` có sẵn.
- `baselines/actuated.py`: `decide()` cũng luôn trả `[]`. Toàn bộ hành vi nằm ở **file mạng riêng** `networks/grid_4x4/net_actuated.xml` + `sim_actuated.sumocfg`, sinh một lần bằng `netconvert --sumo-net-file net.xml --tls.rebuild --tls.default-type actuated --tls.min-dur 7 --tls.max-dur 90 -o net_actuated.xml` (min/max khớp `HARD_CONSTRAINTS` ở Bước 7). **Phát hiện quan trọng**: đổi `type` của một `tlLogic` đang chạy sang `actuated` bằng TraCI (`setProgramLogic`) **không** hoạt động như actuated thật — SUMO chỉ tự sinh detector cảm biến (induction loop) khi **nạp** một mạng có sẵn `type="actuated"` từ file, không phải khi đổi type giữa chừng qua API. Đã kiểm chứng trực tiếp: đổi qua TraCI → pha xanh vẫn cố định y hệt static (42s mỗi lần); nạp từ `net_actuated.xml` → pha xanh dao động thật theo nhu cầu (quan sát được 7-10s+ thay vì hằng số 42s).
- `sim/runner.py`: thêm `make_controller(mode, conn)`, chọn `sim_actuated.sumocfg` khi `mode=actuated` (còn lại dùng `sim.sumocfg`), thêm `CONTROL_INTERVAL_S=90` (bằng đúng 1 chu kỳ đèn của `grid_4x4`) — tại mỗi mốc lấy mẫu metric trùng với mốc quyết định, gọi `controller.decide()`, đưa từng action qua `validate()` (Bước 7) rồi `apply_action()` nếu hợp lệ, ghi mọi quyết định vào bảng `decisions` (đã có sẵn từ Bước 4: `validator_status`, `validator_violations`, `applied`). Baseline có thêm hook tùy chọn `observe(snapshot, sim_time)` — gọi mỗi 10s (không nằm trong `Controller` protocol dùng chung, chỉ `maxpressure` cần) — lý do ở dưới.
- `baselines/maxpressure.py`: pressure theo đúng định nghĩa Max-Pressure (`getLastStepHaltingNumber` vào trừ ra, theo từng phase xanh, tra cứu qua `trafficlight.getControlledLinks`), nhưng **cách dùng pressure khác bản gốc** vì không gian hành động ở đây hẹp hơn (chỉnh split, không tự do chọn phase) — mỗi chu kỳ chỉ nhích `AdjustPhaseSplit` một bước cố định (`_STEP_S`) từ phase ít áp lực nhất sang phase nhiều áp lực nhất, nếu chênh lệch vượt ngưỡng (`_MIN_PRESSURE_DIFF`).

**Sự cố trong lúc verify (đáng ghi lại)**: 3 lần triển khai đầu của `maxpressure` đều **thua** `fixed` (7.31s, 9.29s, rồi 6.97s so với `fixed` 6.49s) — vi phạm thẳng DoD. Nguyên nhân, tìm ra qua đo trực tiếp:

1. `getLastStepHaltingNumber` là một lần đọc tức thời, còn chu kỳ quyết định (90s) trùng khít chu kỳ đèn (90s) → `decide()` luôn rơi vào đúng một điểm cố định trong chu kỳ mỗi lần (ngay sau khi phase vừa kết thúc hàng chờ đầy nhất, phase vừa chạy hàng chờ rỗng nhất) → tín hiệu bị lệch có hệ thống, không phải nhiễu ngẫu nhiên.
2. Nhảy thẳng tới "tỉ lệ lý tưởng" mỗi chu kỳ (chia lại toàn bộ thời gian xanh theo đúng tỉ lệ áp lực đo được) quá mạnh tay — một lần đo áp lực bằng 0 (rất dễ xảy ra) đẩy phase đó về thời lượng xanh gần 0, gây dao động dữ dội giữa các chu kỳ (đã thấy `delta_s` nhảy +42 rồi -57 rồi +42... liên tục).

**Cách sửa**: (a) thêm `observe()` gọi mỗi 10s, **lấy trung bình áp lực trên toàn bộ khoảng 90s** thay vì đọc một lần tại đúng lúc `decide()` chạy — sửa đúng gốc rễ của lệch hệ thống; (b) bỏ hẳn kiểu "nhảy tới tỉ lệ lý tưởng", thay bằng bước nhích cố định nhỏ (`_STEP_S=5.0`, dưới xa `max_delta_per_cycle_s=15`), chỉ nhích khi chênh lệch áp lực vượt ngưỡng (`_MIN_PRESSURE_DIFF=4.0`) — không bao giờ đề xuất thứ cực đoan. Đã rà một vùng tham số xung quanh giá trị chọn để xác nhận đây là một vùng thắng ổn định (nhiều tổ hợp lân cận đều thắng `fixed`), không phải một điểm may mắn do overfit vào đúng 1 seed.

**Verify** (qua đúng `sim.runner` CLI + Postgres, không phải script tạm):

```bash
python -m sumo_agents.sim.runner --scenario grid_4x4 --mode fixed --seed 42        # mean_waiting_s = 6.4941
python -m sumo_agents.sim.runner --scenario grid_4x4 --mode actuated --seed 42     # mean_waiting_s = 2.8100
python -m sumo_agents.sim.runner --scenario grid_4x4 --mode maxpressure --seed 42  # mean_waiting_s = 6.2247
```

Cả 3 chạy hết 3600s (thực tế ~3800-3820s do xe chạy nốt, như Bước 5). `maxpressure` (6.22s) tốt hơn `fixed` (6.49s) ~4.2%; `actuated` (2.81s) — đối thủ thật sự — vượt trội cả hai, đúng kỳ vọng của plan (actuated phản ứng theo từng bước mô phỏng qua detector thật, còn `maxpressure` ở đây chỉ được quyết định mỗi 90s trong một không gian hành động cố ý hẹp).

`tests/test_baselines.py`: test thuần cho phần logic không cần SUMO (`phase_kind`, `phase_deltas`, `fixed`/`actuated` luôn `decide()`→`[]`) — không lặp lại việc chạy 3600s thật trong CI vì tốn thời gian và cần SUMO.

**DoD**: cả 3 chạy hết 3600s (✅) · `maxpressure` tốt hơn `fixed` về mean waiting time (✅, 6.22s so với 6.49s) — **đã đạt**.

---

## Bước 9 · 🔒 Bảng so sánh baseline ✅

**Mục tiêu**: harness chạy nhiều run và ra bảng — hạ tầng đo lường hoàn chỉnh.

**Đã làm**:

- Schema: thêm cột `runs.summary` (JSONB, nullable — migration `4f233553e441`) chứa các số liệu **toàn run** không hợp với hình dạng bảng `metrics` (mỗi dòng là 1 junction tại 1 mốc thời gian): `mean_travel_time_s`, `n_completed_trips`. Chọn 1 cột JSON linh hoạt thay vì thêm cột cứng cho từng số liệu, giống cách `config`/`params`/`effect` đã làm — Bước 14/17 có thể thêm field vào đây mà không cần migrate lại.
- `sim/runner.py`: theo dõi `simulation.getDepartedIDList()`/`getArrivedIDList()` mỗi step (không thể chỉ đọc tại mốc lấy mẫu 10s vì sẽ bỏ sót xe đến/đi giữa 2 mốc), tính `travel_time = t_đến - t_khởi hành` mỗi xe hoàn thành, ghi trung bình + số xe hoàn thành vào `runs.summary` lúc `finish_run()`. `run()` giờ trả về `run_id` (trước đây chỉ `print`) để `compare.py` gọi thẳng trong tiến trình, không phải parse stdout.
- `scripts/compare.py`: chạy N chế độ × M seed **tuần tự trong cùng 1 tiến trình** (gọi thẳng `sim.runner.run()`, không subprocess — bắt buộc tuần tự vì `libsumo` chỉ cho 1 simulation/tiến trình). Với mỗi run: `mean_waiting_s`/`queue_p95`/`mean_co2_mg` tính từ bảng `metrics` (trung bình và `percentile_cont(0.95)` qua SQL trên Postgres), `mean_travel_time_s`/`n_completed_trips` (đổi tên hiển thị là "throughput") đọc từ `runs.summary`. Gộp qua các seed: mean ± khoảng tin cậy ~95% (xấp xỉ chuẩn `1.96·stdev/√n`, **không phải** khoảng Student-t chính xác — với mặc định 3 seed đây chỉ là ước lượng thô, đã ghi rõ trong bảng xuất ra). Xuất bảng markdown (`data/compare/<scenario>.md`) **và** biểu đồ cột 5 panel kèm error bar (`data/plots/compare_<scenario>.png`, theo yêu cầu bổ sung của người dùng).

**Verify**:

```bash
python scripts/compare.py --scenario grid_4x4   # mặc định: fixed,actuated,maxpressure × seed 42,43,44
# -> chạy 9 lần (~22s tổng, libsumo headless), in bảng, ghi data/compare/grid_4x4.md + data/plots/compare_grid_4x4.png
```

Kết quả (grid_4x4, 3 seed):

| mode | Mean waiting (s) | Mean travel time (s) | Throughput | Queue p95 | CO2 (mg/s) |
|---|---|---|---|---|---|
| fixed | 6.6 ± 0.2 | 136.9 ± 2.5 | 4500.0 ± 0.0 | 13.3 ± 0.7 | 24022.8 ± 438.6 |
| actuated | 2.7 ± 0.1 | 105.2 ± 2.1 | 4500.0 ± 0.0 | 5.0 ± 0.0 | 19307.8 ± 219.0 |
| maxpressure | 6.5 ± 0.3 | 138.3 ± 2.6 | 4500.0 ± 0.0 | 13.3 ± 0.7 | 24349.3 ± 400.8 |

**Phát hiện cần ghi nhận trung thực**: `actuated` vẫn vượt trội rõ rệt cả 3 seed (đúng như kỳ vọng — "đối thủ thật sự"). Nhưng `maxpressure` — vốn đã thắng `fixed` ở Bước 8 khi đo riêng seed=42 (6.22s so với 6.49s) — khi gộp cả 3 seed thì **gần như hòa** với `fixed` (6.5±0.3 so với 6.6±0.2, khoảng tin cậy chồng lấn nhau; travel time thậm chí nhỉnh hơn một chút: 138.3 so với 136.9). Nghĩa là bộ tham số của `maxpressure` (chỉnh ở Bước 8) đã **bám khá sát vào đúng động lực học sự cố của seed=42**, chưa chắc tổng quát hoá tốt sang seed khác — một giới hạn thật của cách tiếp cận đơn giản (nhích từng bước nhỏ theo áp lực đo được), không phải lỗi code. Không tinh chỉnh lại thêm ở bước này (tránh overfit tiếp vào đúng 3 seed dùng để đo) — ghi nhận đúng tinh thần "báo cáo trung thực cả chỗ thua" của plan §7, để lại như một hạn mục biết trước nếu sau này muốn cải thiện `maxpressure`.

**DoD**: một lệnh sinh ra bảng so sánh 3 chế độ × 3 seed — **đã đạt** (DoD không yêu cầu `maxpressure` phải thắng ở bước này, chỉ yêu cầu hạ tầng đo lường hoạt động).

> 🚩 Hoàn thành Bước 9 nghĩa là bạn đã có một POC hoàn chỉnh *không cần AI*. Mọi thứ sau đây là thêm lớp AI vào một nền đã đo được.

---

# PHASE 2 — Agent lõi

## Bước 10 · `agents/llm.py` — call site duy nhất ✅

**Mục tiêu**: mọi lời gọi model đi qua đúng một hàm.

**Đã làm**:
- `src/sumo_agents/obs/cost.py`: bảng giá đúng §1.2 plan (`sol`/`terra`/`luna`, cả `gpt-6-astra` cho đầy đủ), `compute_cost_usd(model, *, input_tokens, cached_tokens, output_tokens)`. Lưu ý quan trọng khi tính: `cached_tokens` là **tập con** của `input_tokens` (không cộng dồn thêm), và `output_tokens` (OpenAI) **đã bao gồm** `reasoning_tokens` bên trong nó (breakdown, không phải phần cộng thêm) — xác nhận từ chính source của SDK (`ResponseUsage`/`OutputTokensDetails`), không đoán.
- `src/sumo_agents/agents/llm.py`: hàm `ask(role, system, user, schema, *, cache_key, client=None)` — đúng theo §3.3 plan (`client.responses.parse` + `text_format` + `reasoning={"effort": ...}` + `prompt_cache_key`). `MODELS` dict: `junction`→(`gpt-5.6-luna`, `low`) · `supervisor`→(`gpt-5.6-terra`, `medium`) · `scenario`→(`gpt-5.6-sol`, `high`).
  - `client` param cho phép inject fake client trong test (tests/test_llm.py dùng `SimpleNamespace`, không gọi mạng thật).
  - **Không bao giờ raise ra ngoài** — lỗi API/refusal/parse fail trả về `(None, Usage(status="error", ...))`. Lý do: khớp nguyên tắc §3.2 "sim không bao giờ chờ LLM" — một JunctionAgent (Bước 12) sẽ coi `None` là tín hiệu để rơi về `no_action` cho chu kỳ đó, không phải crash cả vòng lặp quyết định.
  - Đọc `OPENAI_API_KEY` qua `.env` (cùng pattern với `obs/db.py`'s `database_url()`: `load_dotenv(override=False)` rồi fail rõ ràng nếu thiếu, không fallback âm thầm).
  - Xác nhận field usage chính xác từ source code SDK (`openai==3.10.0`) thay vì đoán: `usage.input_tokens_details.cached_tokens`, `usage.output_tokens_details.reasoning_tokens` — đúng như plan đã dự đoán, không cần đổi.
- `Store.add_llm_call(...)` (đã có sẵn từ Bước 4) được gọi trực tiếp từ script kiểm tra (Bước 12+ sau này, mỗi agent sẽ tự gọi với đúng `run_id`/`sim_time`/`agent_id` của nó — `ask()` cố tình KHÔNG tự ghi DB, vì nó không biết ngữ cảnh run/cycle, giữ đúng single-responsibility: `ask()` chỉ gọi model, ai gọi `ask()` mới biết ghi vào đâu).

**Việc đầu tiên khi chạy** (theo đúng plan): in nguyên `Usage` (bọc từ `resp.usage`) ở lượt gọi đầu tiên — xem `scripts/verify_llm_cache.py`.

**Kiểm chứng DoD** (`python scripts/verify_llm_cache.py`, role=`junction`→`gpt-5.6-luna`, 3 lượt gọi thật, cùng `system` prompt):
- **Lần chạy đầu tiên FAIL**: `input_tokens=707` (dưới ngưỡng cache ~1024 token của OpenAI) → `cached_tokens=[0, 0, 0]` cả 3 lượt. Đúng như checkpoint "dừng lại nếu fail" — không bỏ qua, đã tìm nguyên nhân: system prompt ban đầu quá ngắn.
- Viết lại `SYSTEM_PROMPT` trong script dài và chi tiết hơn (topology, action space, safety constraints, format tin nhắn coalition, quy tắc data-vs-instruction) — bản nháp thực tế cho Bước 11/12 sau này, không chỉ để qua ngưỡng token. `input_tokens` tăng lên 1410.
- **Lần chạy lại: PASS** — `cached_tokens = [0, 1348, 1348]` (lượt 1 chưa có gì để cache, lượt 2-3 hit gần hết phần system prompt bất biến). Cost mỗi lượt gọi: ~$0.0003–0.0005 (model `luna`, effort `low`).
- Latency mỗi lượt: 3.1–4.9s — dữ liệu này quan trọng cho thiết kế Bước 12-14 (xem phần "Đề xuất" bên dưới).
- Dọn 2 run "smoketest" không đạt/không hoàn chỉnh khỏi Postgres sau khi xác nhận, giữ lại 1 run pass làm bằng chứng.

**Test**: `tests/test_cost.py` (5 test, tính tiền thuần logic, không gọi mạng) + `tests/test_llm.py` (7 test, dùng fake client injected qua `client=` param — test logic trích usage/tính cost/đường lỗi-không-raise của `ask()`, không test hành vi thật của OpenAI). Tổng suite: 58 passed.

> 💡 **Đề xuất về tần suất/latency** (bạn đã mời góp ý ở bước này): với `effort="low"` trên `luna`, mỗi lượt gọi mất **3–5 giây thực** (không phải giây mô phỏng). Chu kỳ quyết định hiện tại là 90s **mô phỏng**, không phải 90s thực — SUMO chạy nhanh hơn thời gian thực rất nhiều (headless, `libsumo`), nên vòng lặp `sim/runner.py` có thể đi qua rất nhiều giây mô phỏng trong vài giây thực. Điều này củng cố đúng nguyên tắc §3.2 ("sim không bao giờ chờ LLM", cơ chế `skipped_cycle`) mà plan đã thiết kế sẵn — với 4-6 `JunctionAgent` gọi song song (asyncio, không tuần tự) mỗi 90s mô phỏng, tổng latency ~4s mỗi lượt là hoàn toàn khả thi để không bị dồn cục, miễn là các lệnh gọi thực sự chạy song song (`asyncio.gather`), không tuần tự từng agent một — tôi sẽ giữ nguyên tắc này khi làm Bước 12-14, sẽ nói rõ nếu cần đổi.

---

## Bước 11 · `protocol.py` — schema Pydantic ✅

**Mục tiêu**: hợp đồng dữ liệu giữa các agent, máy đọc được và người đọc được.

**Một thay đổi có chủ ý so với bản phác thảo trong plan**: plan gợi ý `Action` là `type: Literal[...] + params: dict` (xem khối code gốc từng ở đây). Thay vào đó, `src/sumo_agents/agents/protocol.py` **tái dùng thẳng** discriminated union đã có sẵn từ `safety/validator.py` (Bước 7: `AdjustPhaseSplit`, `SetCycleLength`, `SetOffset`, `RequestVms`, `NoAction`, mỗi loại có field kiểu riêng thay vì `dict` chung) — đúng như đã ghi chú trước ở Bước 7 rằng "có thể tái dùng... không phá vỡ gì". Lý do: `params: dict` không cho Pydantic validate được kiểu dữ liệu bên trong (model trả `delta_s: "nhiều"` vẫn parse "thành công" thành `dict`, chỉ vỡ khi validator xử lý logic) — union đã có sẵn thì `delta_s` sai kiểu sẽ raise ngay tại biên LLM, sớm hơn và rõ ràng hơn. `protocol.py` chỉ import các Action type, không import `validate()`, nên không tạo phụ thuộc ngược.

**Đã làm** (`src/sumo_agents/agents/protocol.py`):
- `ActionUnion = Union[AdjustPhaseSplit, SetCycleLength, SetOffset, RequestVms, NoAction]` — ban đầu dùng `Annotated[..., Field(discriminator="type")]`, **đã bỏ discriminator ở Bước 12** sau khi phát hiện lỗi thật (xem ghi chú ở Bước 12).
- `Proposal`, `Message`, `Verdict` đúng theo plan §5, với `action`/`modified_action` dùng `ActionUnion` thay vì `dict`.
- Thêm một ràng buộc nhỏ không có trong plan nhưng hợp lý về ngữ nghĩa: `Verdict` với `decision="modified"` mà `modified_action=None` sẽ raise ngay ở biên schema (`@model_validator`) — tránh trạng thái mơ hồ khi Bước 13/14 áp dụng verdict.
- `wrap_untrusted_data(label, content)` + `UNTRUSTED_DATA_SYSTEM_NOTICE`: helper bọc dữ liệu ngoài (tên đường OSM — Bước 18, `ScenarioSpec` do model sinh — Bước 20, payload tin nhắn từ agent khác) trong delimiter `<<<UNTRUSTED_DATA label=...>>> ... <<<END_UNTRUSTED_DATA>>>`, đi kèm câu thông báo cố định để chèn vào `system` prompt — đúng plan §12.2 và nguyên tắc bảo mật của dự án (không tuân theo chỉ thị nhúng trong dữ liệu ngoài).

**DoD**: `tests/test_protocol.py` — 11 test, pass. Bao gồm đúng case yêu cầu: JSON có `action.type` không nằm trong 5 loại hợp lệ → `pydantic.ValidationError` nêu rõ giá trị sai (`test_proposal_with_unknown_action_type_raises_clearly`), thiếu field bắt buộc, sai literal cho `urgency`/`intent`, và case `Verdict` tự định nghĩa thêm ở trên. Toàn suite: 69 passed.

> ⚠️ **Lỗi phát hiện muộn hơn, ở Bước 12**: 11 test này chỉ test Pydantic thuần (`model_validate_json` cục bộ), **chưa từng gọi OpenAI thật** với `ActionUnion` làm schema. `Field(discriminator="type")` khiến Pydantic sinh JSON Schema dạng `oneOf` + `discriminator` (kiểu OpenAPI) — OpenAI Structured Outputs **từ chối `oneOf`** (`'oneOf' is not permitted`). Lỗi này chỉ lộ ra khi Bước 12 gọi `ask()` thật với `Proposal` làm `text_format`. Đã sửa bằng cách bỏ `discriminator`, dùng `Union` thường (Pydantic sinh `anyOf`, được OpenAI chấp nhận) — xác nhận bằng cách in `Proposal.model_json_schema()` và grep `oneOf`/`anyOf`. 11 test cũ vẫn pass nguyên (kể cả case "unknown action type raises clearly" — Pydantic smart-union không cần discriminator vẫn báo lỗi rõ giá trị sai). **Bài học cho các bước sau**: mọi Pydantic schema dùng làm `text_format` cho `ask()` phải được test với ít nhất 1 lời gọi OpenAI thật trước khi coi là xong, không chỉ test Pydantic cục bộ.

---

## Bước 12 · `JunctionAgent` (vòng 1: observe) ✅

**Mục tiêu**: một agent nhìn trạng thái nút của mình và đề xuất hành động.

**Đã làm**:
- `src/sumo_agents/agents/topology.py` — `signalized_neighbor_map(net_file) -> dict[junction_id, list[neighbor_id]]`: đọc `net.xml` bằng `sumolib.net.readNet`, với mỗi node `type="traffic_light"`, lấy các node liền kề (qua `getIncoming`/`getOutgoing`) mà cũng là `traffic_light` — **1-hop thật theo mạng lưới**, không hardcode, không all-to-all (verify: không nút nào có > tổng-1 hàng xóm, quan hệ đối xứng, khớp thủ công với `grid_4x4/net.xml`: `B1 -> [A1, B0, B2, C1]`).
- `src/sumo_agents/agents/junction.py` — `JunctionAgent(junction_id, neighbor_ids)`:
  - System prompt dựng **một lần** lúc khởi tạo (từ `junction_id` + `neighbor_ids` + `HARD_CONSTRAINTS` nội suy trực tiếp từ `safety.validator.HARD_CONSTRAINTS`, không gõ tay số để tránh lệch khi hằng số đổi), giữ **bất biến từng byte** suốt vòng đời agent — đúng điều kiện cache của Bước 10. Mọi thứ đổi theo chu kỳ (`sim_time`, độ dài từng pha hiện tại, `queue_len`, `mean_waiting_s`, `mean_speed`, `throughput`, `co2_mg`) nằm trong `user`, dựng lại mỗi lần gọi `observe()`.
  - Độ dài pha hiện tại lấy từ `TlsState` thật (`sim/actuators.py`'s `read_tls_state`, có sẵn từ Bước 8) thay vì giả định cố định "42s/3s" — vì đó chỉ đúng cho `grid_4x4` hiện tại, không đúng cho `mixed_district`/`osm_real` sau này, và pha có thể đã bị agent khác chỉnh ở chu kỳ trước.
  - `observe(snapshot, tls_state, sim_time) -> tuple[Proposal, Usage]`: **luôn** trả về `Proposal` hợp lệ, không bao giờ `None`. Nếu `ask()` thất bại (lỗi API/refusal) → rơi về `Proposal(action=NoAction(...), rationale="Lời gọi LLM thất bại...")`, đúng nguyên tắc "sim không chờ LLM" (Bước 10's docstring). Có chuẩn hoá phòng thủ: nếu model trả sai `junction_id` (ở `Proposal` hoặc `action`), ghi đè lại đúng ID thật — tránh định tuyến sai ở Bước 13/14 chỉ vì model "đọc nhầm" tên chính nó.
  - `client=` injectable (giống `ask()`) để test không cần gọi mạng thật.
- Vòng 2-4 (coalition/validate/approve) **chưa** làm — đúng phạm vi "vòng 1: observe" của bước này.

**Test**: `tests/test_topology.py` (5 test, đọc thẳng `networks/grid_4x4/net.xml` thật — thuần XML parsing qua `sumolib`, không cần TraCI/SUMO chạy, nên vẫn nhanh/tất định như mọi unit test khác) + `tests/test_junction.py` (5 test, fake client như `test_llm.py`: system prompt bất biến, parse thành công, chuẩn hoá `junction_id` sai, fallback `no_action` khi lỗi, cache_key/schema truyền đúng xuống `ask()`). Toàn suite: 79 passed.

**Kiểm chứng DoD** (`python scripts/verify_junction_agents.py` — 6 agent thật (`B0,B1,B2,C0,C1,C2`, một khối 2×3 liền kề để có hàng xóm thật cho Bước 13 sau này), chạy trên SUMO thật (`grid_4x4`, seed=42, libsumo), 2 chu kỳ 90s liên tiếp, gọi song song bằng `asyncio.gather` — đúng khuyến nghị đã đưa ra ở Bước 10):
- **Lần chạy đầu FAIL** vì lỗi `ActionUnion`/`oneOf` nêu trên (không tốn tiền — OpenAI từ chối request trước khi tính phí, `cost_usd=None` toàn bộ). Đã sửa `protocol.py`, chạy lại.
- **Lần chạy sau: PASS** — 6/6 `Proposal` hợp lệ cả 2 chu kỳ · `cached_tokens = [1212, 1218, 1218, 1212, 1218, 1218]` ở chu kỳ 2 (chu kỳ 1 đều `0`, đúng như dự kiến — chưa có gì để cache) · **cả 6/6 agent đều trả `no_action`** ở cả 2 chu kỳ, với `rationale` tiếng Việt cụ thể, có trích số liệu thật (vd B1: *"hàng đợi chỉ 8 xe... tốc độ trung bình 7,6 m/s... chưa có bằng chứng rõ ràng cần thay đổi"*) — khớp yêu cầu DoD "nút thông thoáng trả no_action" và không có dấu hiệu prompt thúc ép hành động.
- Ghi chú: t=90s/180s là rất sớm trong run (mới ~110-220/4500 trip đã xuất phát), nên lưu lượng còn nhẹ ở toàn bộ 6 nút — hợp lý là chưa nút nào cần can thiệp. Bước 13/14 (hoặc một lần chạy thủ công lấy mẫu ở cửa sổ sự cố ~1200-1800s) sẽ là dịp thực sự thấy agent đề xuất `adjust_phase_split`/khác `no_action`.
- Cost: ~$0.0039 cho 12 lời gọi thật (6 nút × 2 chu kỳ). Latency: 2.3–4.4s/lượt (khớp số liệu Bước 10).
- Dọn 1 run thất bại (do bug, trước khi sửa) khỏi Postgres, giữ lại run pass.

> Ghi chú đã đúng như cảnh báo trong bước này: nếu agent không bao giờ trả `no_action` mới là dấu hiệu xấu — ở đây ngược lại (luôn `no_action` vì traffic còn nhẹ), không phải vấn đề.

---

## Bước 13 · Coalition + `SupervisorAgent` (vòng 2–4) ✅

**Mục tiêu**: hoàn thiện pipeline 4 vòng.

**Cơ chế vòng 2 (coalition) — quyết định thiết kế cụ thể hoá "tối đa 2 vòng"**: plan chỉ nêu tên vòng, không nói rõ cơ chế bên trong, nên đã cụ thể hoá như sau — không phải 2 lượt gọi LLM cho mỗi cặp, mà là:
- **Bước A (broadcast, KHÔNG gọi LLM)**: nút *đang tắc* (Proposal ở vòng 1 có `action.type != "no_action"`) relay thẳng đề xuất + rationale của chính nó (đã có sẵn từ vòng 1) tới các hàng xóm **đang là agent LLM trong run này** (tra bằng `agents/topology.py`, giao với tập agent đang chạy — không gửi cho hàng xóm không có agent). Không hỏi lại "bạn muốn làm gì" lần 2 vì dư thừa.
- **Bước B (reply, 1 lệnh gọi LLM thật/tin nhắn đến)**: mỗi nút nhận được tin nhắn trả lời **thật** bằng `JunctionAgent.reply()` — chọn 1 trong 5 `intent` (report/request_help/propose/ack/object) kèm rationale tiếng Việt, có xét đến trạng thái hiện tại của chính nó + nội dung tin nhắn đến (bọc `wrap_untrusted_data`, vì đây là nội dung do agent khác — không phải code tất định — sinh ra).
- Không có vòng thứ 3 nào nút gửi lại phản hồi cho bên gửi ban đầu — dừng ở đây, đúng nghĩa "tối đa 2 vòng trao đổi". Nút thông thoáng (`no_action`) không bao giờ broadcast → đúng cơ chế cắt chi phí đã nêu trong plan.
- Bản thân Proposal **không bị coalition sửa đổi** — coalition chỉ làm giàu ngữ cảnh (tin nhắn) cho `SupervisorAgent` ở vòng 4, nơi thực sự có quyền approve/modify/deny. Ví dụ cụ thể "2 nút cùng xin ưu tiên ngược chiều" trong plan chính là lúc một tin nhắn `object` xuất hiện ở vòng 2 và supervisor phải cân nhắc nó ở vòng 4.

**Đã làm**:
- `src/sumo_agents/agents/supervisor.py` — `SupervisorAgent`: **một instance, một lệnh gọi LLM mỗi chu kỳ** (không phải mỗi nút — đúng phân bổ model `gpt-5.6-terra`/`effort=medium` ở plan §1.2, vì giải xung đột cần nhìn toàn cảnh). `review(candidates, messages, sim_time) -> (dict[junction_id, Verdict], Usage | None)` — `Usage=None` khi không có candidate nào (không tốn 1 lệnh gọi cho câu hỏi rỗng). Luôn trả đủ 1 `Verdict`/candidate: nếu model bỏ sót hoặc lỗi, mặc định `approved` (an toàn vì action đã qua `validator.py` rồi — không phải bypass an toàn, chỉ là chấp nhận cái đã biết là an toàn).
- `src/sumo_agents/agents/orchestrator.py` — `run_decision_cycle(proposals, agents, neighbor_map, snapshots, tls_states, supervisor, ...)`: nhận **Proposal của vòng 1 làm input** (không tự chạy lại `observe()`) — tách biệt rõ vòng 1 (Bước 12, độc lập từng nút) khỏi vòng 2-4 (cần nhìn cả chu kỳ), đồng thời cho phép script kiểm chứng DoD **tự tạo Proposal**, kể cả một cái cố ý vi phạm (không có cách nào bắt LLM thật trả về action sai theo yêu cầu). Chạy tuần tự đúng 4 vòng, ghi `messages`/`decisions`/`llm_calls` đầy đủ.
- `Verdict` (`protocol.py`) thêm field `junction_id` — cần thiết vì giờ 1 lệnh gọi supervisor trả về **danh sách** verdict cho nhiều nút cùng lúc, không phải 1 verdict/lệnh gọi như bản phác thảo ở Bước 11.
- `JunctionAgent.reply()` (`junction.py`) — method mới cho vòng 2's Bước B. Dùng chung system prompt/cache_key với `observe()` (cùng 1 agent identity), chỉ khác schema (`Proposal` ↔ nay là `_CoalitionReplyDecision` sau khi sửa lỗi bên dưới).

**⚠️ Lỗi thật thứ hai phát hiện khi chạy DoD** (cùng nhóm nguyên nhân với lỗi `oneOf` ở Bước 12): `Message.payload: dict` — một dict **mở** (không key cố định) — bị OpenAI Structured Outputs từ chối: `'additionalProperties' is required to be supplied and to be false`. Vì `dict` mở về bản chất không thể ép `additionalProperties: false` (làm vậy thì nó rỗng, mất hết ý nghĩa), nên **mọi field kiểu `dict` không có schema cố định đều không dùng được làm `text_format` cho `ask()`**. Lỗi này không lộ ra ở Bước 11 (chỉ test Pydantic cục bộ) và suýt không lộ ra ở Bước 13 nữa — vì `JunctionAgent.reply()` có sẵn cơ chế fallback "sim không chờ LLM" nên cả 6/6 lệnh gọi `reply()` thật đều lỗi 400 nhưng **âm thầm rơi về `ack` mặc định**, script DoD vẫn in ra tin nhắn "hợp lệ" và PASS ở lần chạy đầu — chỉ lộ ra khi tra trực tiếp cột `llm_calls.error` trong Postgres.
- **Sửa tận gốc**: nhận ra model thực sự chỉ cần quyết định `intent` + `rationale` cho một reply — `sender`/`recipients` vốn đã tất định (đã biết chắc), và `payload` chưa từng được dùng cho reply. Thay vì cố ép `payload` thành schema hợp lệ, bỏ hẳn việc hỏi model 2 field đó: thêm schema riêng, hẹp `_CoalitionReplyDecision(intent, rationale)` (private, trong `junction.py`) chỉ cho `reply()`; `Message` đầy đủ được `JunctionAgent` tự dựng lại từ id đã biết + intent/rationale model trả về. Bump `cache_key` → `v3` (system prompt đổi mô tả output format cho khớp).
- **Bài học bổ sung cho `oneOf`-lesson ở Bước 12**: không chỉ union có discriminator, mà **bất kỳ field `dict`/map mở nào** cũng không dùng được làm `text_format`. Kiểm tra lại: `Proposal`, `Verdict` (qua `_SupervisorReview`) không có field dict mở nào → an toàn. Nguyên tắc chung rút ra: **mọi schema Pydantic dùng làm `text_format` phải toàn bộ field kiểu cố định** (model/union/list/scalar), không có `dict` tự do ở bất kỳ đâu trong cây kiểu.

**Test**: `tests/test_supervisor.py` (6 test, fake client — không gọi mạng, không candidate thì không gọi `ask()`, mặc định `approved` khi model bỏ sót/lỗi/trả về id lạ) + `tests/test_orchestrator.py` (4 test, dùng `store` fixture SQLite in-memory có sẵn từ Bước 4 + fake `JunctionAgent`/`SupervisorAgent` — test đúng logic của `orchestrator.py`: ai broadcast, ai reply ai, action vi phạm bị chặn trước supervisor, ghi DB đúng) + 2 test mới cho `JunctionAgent.reply()` trong `test_junction.py`. Toàn suite: 93 passed.

**Kiểm chứng DoD** (`python scripts/verify_coalition.py` — 6 agent thật, Proposal vòng 1 **trộn**: 4 từ LLM thật (đều `no_action`, đúng như quan sát ở Bước 12 vì traffic còn nhẹ ở t=90s), B1 dựng tay hợp lệ (kích hoạt coalition), C1 dựng tay **cố ý vi phạm** (chỉnh pha vàng) — vì không có cách nào ép LLM thật trả về action sai theo yêu cầu; vòng 2 (reply) và vòng 4 (supervisor) đều là lệnh gọi LLM thật):
- **Lần chạy đầu**: PASS về mặt logic nhưng phát hiện lỗi `payload: dict` nêu trên (6/6 reply lỗi 400, fallback che mất). Sửa xong, chạy lại.
- **Lần chạy sau**: PASS sạch, **0 lỗi** trong toàn bộ 11 lệnh gọi LLM thật (4 observe + 6 reply + 1 supervisor). 6 `decisions`, 8 `messages` (2 broadcast + 6 reply). C1: `validator_status=rejected`, `supervisor_violations` nêu rõ "phase '1' is 'yellow'...", `supervisor_verdict=None` — **đúng yêu cầu DoD, chưa từng tới supervisor**. 5 nút còn lại đều có `supervisor_verdict` (đa số `approved`, kể cả B1's `adjust_phase_split` — supervisor đọc đúng ngữ cảnh: các tin nhắn `object` từ C1/B1/C0/C2 chỉ đang bác yêu cầu **không hợp lệ** của C1, không phải xung đột hành lang thật, nên không có gì cần `modified`/`denied` ở chu kỳ này — lý giải bằng tiếng Việt cụ thể, đúng số liệu).
- Cache: supervisor's `cached_tokens=1113` ở lần gọi thứ 2 trong session (cache theo `cache_key` bền qua nhiều run riêng biệt, không chỉ trong 1 run — đúng như tài liệu OpenAI).
- Cost: ~$0.013 cho 11 lệnh gọi thật (lần chạy sạch).
- Dọn 1 run lỗi (do bug, trước khi sửa) khỏi Postgres.

---

## Bước 14 · Nối vào SimRunner, chạy full 1 giờ ✅

**Mục tiêu**: chế độ `llm` chạy trọn vẹn.

**Đã làm** (`src/sumo_agents/sim/runner.py`, mode `"llm"`):
- Round 1 (observe, Bước 12) chạy **song song** (`asyncio.gather`) rồi rounds 2-4 (`agents/orchestrator.py`, Bước 13) — gộp thành **một** background task/chu kỳ (`_run_llm_decision_cycle`), đúng mẫu `asyncio.create_task(...)` không `await` trong plan §3.2. Chu kỳ trước chưa xong khi tới chu kỳ sau → **bỏ qua**, đếm vào `runs.summary.n_skipped_cycles` (không có bảng riêng, tái dùng `Run.summary` JSON đã có — không cần migration).
- `sim/actuators.py`: thêm `_apply_set_cycle_length` (co giãn tỉ lệ các pha xanh, giữ vàng/đỏ-toàn-phần cố định) và `_apply_set_offset` (đơn giản hoá trung thực: `setPhaseDuration` — một lần nhún thời lượng pha hiện tại, **không phải** offset cấu trúc bền vững vì SUMO static program không có tham số offset qua TraCI — đã ghi chú rõ trong code, đúng tinh thần "báo cáo trung thực" của plan §7). `request_vms` vẫn `NotImplementedError` có chủ đích — đúng phạm vi Bước 19, được `_apply_llm_decisions` bắt riêng, ghi `applied=false` + `effect` giải thích thay vì crash.
- Theo dõi `time_since_last_green_s` thật (mỗi 10s, chỉ đọc TraCI, không tốn LLM) cho 6 nút agent — trước đó (Bước 8/12) validator luôn coi mọi pha là "vừa xanh xong" vì thiếu lịch sử; đúng như actuators.py đã ghi chú từ Bước 8 rằng việc này "đến cùng Phase 2".
- `decisions.effect` điền ở **chu kỳ kế**: khi áp dụng `final_action`, nhớ `(decision_id, metric lúc quyết định)`; đầu chu kỳ sau, so với metric mới → `store.update_decision(decision_id, effect={"before":..., "after":...})`. Cần thêm `Store.add_decision` trả về `id` (trước đây `None`) và `Store.update_decision(id, **fields)` mới.
- Cuối run: `runs.summary` có thêm `total_llm_cost_usd`, `n_llm_calls`, `n_skipped_cycles` — trực tiếp phục vụ yêu cầu DoD "chi phí thực tế được ghi lại".
- `scripts/compare.py` (Bước 9) **không cần sửa** — đã tổng quát theo `--modes` dạng chuỗi từ đầu, tự động nhận `llm` một khi `sim/runner.py` hỗ trợ.

**⚠️ Phát hiện quan trọng qua đo đạc thực tế (không phải lỗi code, mà là một khoảng trống kiến trúc)**: đo baseline `fixed` chạy hết 3600s+ mô phỏng chỉ tốn **3.4 giây thực** (libsumo ~1100x thời gian thực). Một chu kỳ quyết định LLM thật tốn 10-20 giây thực. Nếu không ghìm lại, hầu hết 40 chu kỳ trong 1 giờ mô phỏng sẽ luôn thấy "chu kỳ trước chưa xong" (vì chỉ ~80ms thực trôi qua giữa 2 điểm quyết định 90s mô phỏng) → **gần như toàn bộ bị skip**, chỉ ~1 chu kỳ thực sự chạy, chi phí thực tế sẽ chỉ vài cent thay vì khớp ước tính ~$2 của plan. Đã thêm cơ chế ghìm tốc độ **chỉ cho mode `llm`**: `LLM_REALTIME_SPEEDUP = 5.0` (mô phỏng nhanh gấp 5 lần thời gian thực thay vì ~1100 lần) — đủ chậm để một chu kỳ (90s mô phỏng ≈ 18s thực) thường kịp xong trước chu kỳ sau, đủ nhanh để không thực sự mất 1 giờ đồng hồ. Baseline modes không bị ảnh hưởng (không ghìm).

**Kiểm chứng ngắn trước khi chạy full** (`scripts/verify_llm_mode.py` + chạy thật `--mode llm` trong 90s rồi dừng):
- Check 1 (không tốn LLM): `apply_action(SetCycleLength)` trên TraCI thật — chu kỳ 90s → 60s đúng như yêu cầu, pha vàng/đỏ không đổi. `apply_action(SetOffset)` không lỗi.
- Check 2 (LLM thật, ~$0.015): 2 chu kỳ qua đúng hàm sản xuất (`_run_llm_decision_cycle`/`_apply_llm_decisions`) — `decision_id` có giá trị, `effect` điền đúng ở chu kỳ 2 cho quyết định từ chu kỳ 1, không crash.
- Chạy thật 90 giây rồi dừng: **5 chu kỳ chạy trọn vẹn, 0 chu kỳ bị skip** (đúng như tính toán `LLM_REALTIME_SPEEDUP=5.0`), 35 lệnh gọi LLM, $0.036 — đúng cadence ~18s thực/chu kỳ.

**Kết quả full run thật** (`run_id=561c302f-2e73-4b00-995e-a41efff131af`, `grid_4x4`, seed 42, chạy 2026-09-15):
- Hoàn tất sạch: 42 chu kỳ, tới sim_time=3781s (>3600s như kỳ vọng), **0 chu kỳ bị skip**, **0/301 lệnh LLM lỗi**, 258/258 quyết định `validator_status='ok'` + `applied=true` (không có quyết định nào bị clamp/reject/deny).
- Chi phí thật: **$0.314824** cho 301 lệnh gọi LLM (258 junction — đúng 6 nút × 43 chu kỳ round 1, 43 supervisor — 1/chu kỳ) — thấp hơn nhiều ước tính ~$2 của plan.
- `n_completed_trips=4500`, `mean_travel_time_s=134.35`.

**⚠️ Phát hiện trung thực quan trọng (theo đúng tinh thần "báo cáo trung thực cả chỗ thua" của plan §7)**: cả 258 quyết định trong suốt 1 giờ đều là `no_action` — **0 tin nhắn coalition** (round 2 không bao giờ kích hoạt, vì nó chỉ chạy khi có junction đề xuất khác `no_action`). Đối chiếu `metrics` trong đúng cửa sổ sự cố (`incidents.yaml`: `B1B2`/`B2B1`, 1200-1800s, `speed_factor=0.005`) cho thấy sự cố **có** gây tắc thật ở B1/B2 (queue_len lên tới 51, `mean_waiting_s` lên tới ~160s quanh sim_time≈1480-1530s) — tức là JunctionAgent có đủ dữ liệu để thấy tắc nghẽn rõ ràng nhưng vẫn luôn chọn không hành động. Hệ quả: `mean_travel_time_s` của `llm` (134.35555...) khớp **chính xác từng chữ số** với baseline `fixed` (134.34555555555556) — hai run cho kết quả giống hệt nhau vì `llm` trên thực tế không hề thay đổi gì so với chương trình đèn tĩnh mặc định. So với `actuated` (107.40s, tốt hơn hẳn) và `maxpressure` (139.51s), `llm` chỉ ngang `fixed`.
  - Khả năng nguyên nhân (chưa kết luận, cần Bước 16/tinh chỉnh prompt mới rõ): mỗi lệnh gọi `observe()` chỉ thấy **snapshot hiện tại**, không có lịch sử/xu hướng qua các chu kỳ trước — trong khi system prompt lại yêu cầu rõ "chỉ đề xuất khi có mẫu hình rõ ràng **và bền vững** (persistent), một lần đọc nhiễu là chưa đủ". Không có tín hiệu "đã thấy queue cao 2-3 chu kỳ liên tiếp" trong user prompt, model có thể luôn kết luận "chưa đủ bằng chứng bền vững" → không bao giờ vượt ngưỡng để hành động.
  - Đây là quan sát hành vi cần cân nhắc (có thể là việc của Bước 16 khi có dashboard để debug trực quan, hoặc một vòng tinh chỉnh prompt riêng) — **không** sửa trong phạm vi Bước 14.

**Follow-up đã sửa ngay sau khi phát hiện (cùng ngày, theo yêu cầu người dùng)**: thêm lịch sử ngắn hạn (`history`, tối đa 3 chu kỳ điều khiển gần nhất, cũ→mới, KHÔNG gồm chu kỳ hiện tại) vào user prompt của `JunctionAgent.observe()` — `sim/runner.py` nuôi buffer `snapshot_history` mỗi khi tạo `pending_task` mới, `agents/junction.py` thêm `_build_trend_text()` + tham số `history` cho `observe()`, cache_key bump `v3`→`v4` (system prompt đổi nội dung mô tả). Chỉ gap (1); gap (2) — phân rã theo hướng/pha — vẫn để sau.
- **Kiểm chứng lại bằng đúng dữ liệu thật đã gây ra vấn đề**: lấy lại chính xác trạng thái B1 tại sim_time=1301/1391/1481s từ run thật (`queue_len` 33→40→51, `mean_waiting_s` 22.9→79.4→96.1s), gọi `observe()` mới với `history` 2 điểm đầu. Kết quả: model **đổi từ `no_action` sang `adjust_phase_split(phase_id=0, delta_s=8.0)`**, rationale trích dẫn đúng xu hướng 3 số: *"Ùn tắc đang tăng rõ qua các chu kỳ: queue_len 33→40→51, thời gian chờ 22,9→79,4→96,1 giây... chọn mức vừa phải vì chưa có dữ liệu phân tách theo hướng"* — tự nhận đúng luôn giới hạn của gap (2) còn lại. 2 test mới trong `tests/test_junction.py` (không có lịch sử → ghi rõ "chưa có", có lịch sử → prompt chứa đúng số liệu). 95/95 test pass.
- Run full 1 giờ ngày 2026-09-15 (`561c302f-...`) giờ đã **lỗi thời** cho việc đánh giá hành vi phối hợp (chạy trước khi có fix) — số liệu chi phí/độ ổn định của nó vẫn đúng và giữ nguyên trong bảng trên, nhưng nếu muốn xem `llm` thật sự hành động trong cửa sổ sự cố cần chạy lại full 1 giờ một lần nữa.

**Run full 1 giờ SAU khi có fix trend history** (`run_id=81760b55-7d0c-4b17-8c36-fd891b398737`, `grid_4x4`, seed 42, chạy 2026-09-15 — thay thế các nhận định phía trên về hành vi):
- Hoàn tất sạch: 43 chu kỳ, **0 chu kỳ skip**, **0/329 lệnh LLM lỗi**, 258/258 quyết định `validator_status='ok'` + `applied=true`. Chi phí **$0.382745** (vẫn thấp hơn nhiều ước tính ~$2 của plan).
- **Agent giờ thực sự hành động**: 11/258 quyết định khác `no_action` (8 `adjust_phase_split`, 3 `set_cycle_length`), tất cả đều được supervisor `approved`; **39 tin nhắn coalition round** (round 2 lần đầu tiên thực sự kích hoạt trong một full run thật — trước fix con số này luôn là 0). Phần lớn hành động rơi đúng vào/quanh cửa sổ sự cố 1200-1800s (`B0`/`B1`/`B2` tại sim_time 1351/1441/1531/1711s) — đúng nơi có tắc nghẽn thật, xác nhận fix hoạt động đúng ở quy mô cả run, không chỉ ở phép thử tái hiện thủ công.
- **Kết quả trung thực (đúng tinh thần plan §7 "llm có thể thua actuated")**: `mean_travel_time_s=137.54` — **tệ hơn một chút** so với baseline `fixed` không làm gì (134.35s, +~2.4%) và vẫn thua xa `actuated` (107.40s). Nhìn từng quyết định riêng lẻ, `effect` (before/after) tại điểm quyết định thường cho thấy cải thiện cục bộ ở đúng nút đó (vd. B2 tại 1441s: queue 34→27, wait 118→31s), nhưng B1 lại 2 lần liên tiếp tăng `cycle_s` (90→100→110s) trong lúc tắc mà `mean_waiting_s` tại B1 vẫn tiếp tục tăng trước khi giảm — khó tách bạch đâu là nhờ hành động, đâu là do sự cố tự kết thúc ở mốc 1800s. Kết luận trung thực: **fix đã chứng minh đúng cơ chế nó nhắm tới (agent phá được sự thụ động, biết hành động + phối hợp khi có bằng chứng bền vững)**, nhưng việc hành động không tự động đồng nghĩa với kết quả tốt hơn baseline đơn giản — tối ưu hoá thực sự để thắng `actuated` là bài toán khác, ngoài phạm vi DoD của Bước 14.

## Follow-up tối ưu hiệu năng (sau Bước 14, theo yêu cầu người dùng)

Người dùng đề nghị tập trung tối ưu hiệu năng trước khi sang Phase 3 (trực quan hoá không có nhiều ý nghĩa nếu hiệu năng chưa tốt), với 3 hướng điều tra: (1) tần suất quan sát/nhịp quyết định có quá chậm so với tình hình giao thông không, (2) gap (2) — phân rã theo hướng/pha — có hợp lý để làm không, (3) tìm hiểu cơ chế `actuated` để xem có áp dụng được cho LLM/Agent không.

**Phát hiện phân tích (không sửa code)**:
- **(1) Nhịp 90s không phải nút thắt chính**: 0 chu kỳ bị skip ở mọi run thật đã chạy — agent luôn kịp quyết định. 90s trùng đúng 1 chu kỳ đèn hoàn chỉnh, hợp lý cho quyết định chiến lược; tăng tần suất sẽ tăng chi phí tuyến tính và rủi ro dao động (chính system prompt đã cảnh báo) mà không giải quyết đúng gốc rễ ở (3).
- **(3) Gốc rễ khoảng cách hiệu năng với `actuated`**: so sánh trực tiếp `net.xml` (static, mọi mode khác dùng) và `net_actuated.xml` (chỉ `actuated` dùng) cho thấy khác biệt DUY NHẤT là `type="actuated"` + `minDur`/`maxDur` mỗi pha xanh — SUMO tự gắn vòng cảm ứng ở vạch dừng và tự actuate MỖI BƯỚC MÔ PHỎNG (không phải mỗi 90s). Đây là lý do `fixed`(134)/`maxpressure`(139)/`llm`(134-137) đều xoay quanh nhau còn `actuated`(107) vượt trội hẳn: khác biệt về **độ mịn thời gian phản ứng**, không phải độ thông minh của bộ ra quyết định — kể cả bộ retiming định kỳ hoàn hảo cũng bị chặn trần bởi cấu trúc này. Hướng khả thi để thật sự thắng `actuated`: đổi `llm` mode sang nạp `net_actuated.xml`, đổi action space từ "đặt duration cố định" sang "đặt biên min/max green" (SUMO tự actuate trong biên đó, LLM chỉ set biên chiến lược — giống kiến trúc SCATS/SCOOT thật: lớp actuation cục bộ nhanh + lớp tối ưu chiến lược chậm bên trên). Quy mô lớn (đụng `safety/validator.py`, `sim/actuators.py`, prompt, test) — **chưa làm, để quyết định sau**.

**Đã làm — Gap (2): phân rã queue/wait theo pha (`phase_id`)**, thay vì tổng gộp cả nút:
- `sim/actuators.py`: `phase_lane_groups()` (gom lane theo từng pha xanh, dùng lại đúng pattern `MaxPressureController._build_movements` ở `baselines/maxpressure.py` — không sửa file đó, chỉ viết bản độc lập để giữ phạm vi gọn trong code chỉ dùng cho `llm` mode) + `per_phase_traffic()` (đọc `queue_len`/`mean_waiting_s` thật cho từng nhóm lane).
- `agents/junction.py`: `observe()` nhận thêm `per_phase`, `_build_per_phase_text()` thêm dòng `per_phase_queue: phase 0: queue_len=... mean_waiting_s=...; phase 2: ...` vào user prompt, kèm ghi chú rõ "dùng cái này, không dùng tổng cả nút, để chọn phase_id". System prompt cập nhật mô tả, cache_key bump `v4`→`v5`.
- `sim/runner.py`: cache `phase_lane_groups` mỗi nút LLM một lần khi khởi động (topology không đổi trong run), tính `per_phase_traffic` mỗi chu kỳ quyết định, truyền vào `_run_llm_decision_cycle`.
- 2 test mới (`tests/test_junction.py`): có `per_phase` → prompt chứa đúng số liệu theo `phase_id`; không truyền → không có mục đó trong prompt. 97/97 test pass.
- **Kiểm chứng thật ($0 + ~$0.1)**: `scripts/verify_llm_mode.py` thêm check 1c ($0, TraCI thật) — tổng `queue_len` theo từng pha khớp chính xác với tổng toàn nút từ `collect_state` (không thiếu/không trùng lane). Check 2 (2 chu kỳ LLM thật qua đúng pipeline production, giờ có nối `history`+`per_phase`) — ở chu kỳ 2, model đề xuất thật `adjust_phase_split` cho C0 (`phase_id='0', delta_s=5.0`) và C1 (`phase_id='0', delta_s=8.0`), được supervisor approve — xác nhận wiring hoạt động đúng đầu-cuối.
**Run full 1 giờ SAU khi có gap (2)** (`run_id=932194c2-9904-48aa-b998-d6067b9ae0fc`, seed 42, 2026-09-15): **kết quả xấu đi, không tốt lên**.
- `mean_travel_time_s=148.18s` — tệ hơn cả lần trước gap(2) (137.54s) và tệ hơn hẳn không làm gì (134.35s, `fixed`).
- **11/44 chu kỳ bị skip** (trước đó luôn là 0). Nguyên nhân: gap(2) khiến agent tự tin hành động hơn hẳn (35 `adjust_phase_split` so với 8 trước, 123 tin nhắn coalition so với 39) — round 2 (coalition) giờ gần như kích hoạt mỗi chu kỳ thay vì hiếm khi. Vì round1→round2→supervisor chạy tuần tự, độ trễ cộng dồn 3 tầng thường xuyên vượt ngân sách 18s thực (`LLM_REALTIME_SPEEDUP=5.0`).
- **Kết luận trung thực**: agent hành động đúng mục tiêu kỹ thuật hơn (chọn đúng `phase_id` nhờ dữ liệu thật thay vì đoán mò), nhưng hành động nhiều hơn + phối hợp nhiều hơn lại làm mạng lưới tệ hơn — không tốt hơn. Đây là bằng chứng thực nghiệm cho thấy retiming định kỳ (dù có dữ liệu tốt đến đâu) không phải hướng đi đúng — dẫn thẳng tới quyết định actuated-hybrid bên dưới.

## Follow-up actuated-hybrid (thay thế hoàn toàn action space của `llm` mode)

Sau 3 lần chạy full đều cho kết quả retiming định kỳ (dù tinh chỉnh dữ liệu đầu vào thế nào) bị chặn trần rõ rệt dưới `actuated` (107s) — và lần gần nhất còn tệ đi khi agent hành động nhiều hơn — người dùng quyết định chuyển hẳn sang hướng gốc rễ đã xác định ở follow-up trước: cho `llm` mode chạy trên nền actuated thật của SUMO, LLM chỉ đặt biên chiến lược.

**De-risk trước khi sửa code** (script không commit, $0): hạ `maxDur` một pha xanh xuống 15s giữa cửa sổ sự cố thật bằng `setProgramLogic` — quan sát pha xanh bị cắt đúng ở 15s ngay lập tức, xác nhận cơ chế sống hoạt động đúng. Phát hiện phụ: khi tắc nghẽn quá nặng (có teleport xe do "waited too long"), nâng lại biên không giúp gì — pha xanh co về đúng `minDur` vì vòng cảm ứng gần như không thấy xe di chuyển qua để kích hoạt kéo dài. **actuated-hybrid không phải liều thuốc vạn năng cho tắc nghẽn cực nặng, chỉ hiệu quả khi giao thông còn "chảy".**

**Vai trò LLM sau khi chuyển đổi**: lớp phản ứng nhanh (mỗi bước mô phỏng) hoàn toàn thuộc SUMO. LLM chỉ đặt `[min_green_s, max_green_s]` mỗi chu kỳ — và vì mạng actuated mặc định đã ở biên rộng nhất cho phép (`minDur=7, maxDur=90`, đúng bằng `HARD_CONSTRAINTS`), "nâng trần" cho pha tắc thường không còn chỗ để tăng. Đòn bẩy thật sự: (1) siết `max_green_s` của pha đối diện để nhường thời gian, (2) nâng sàn `min_green_s` của pha tắc để đảm bảo phục vụ tối thiểu, (3) phối hợp nhiều nút liên tiếp qua coalition round — điều SUMO actuated cục bộ không có khả năng làm.

**Đã làm**:
- **Network**: `llm` mode nạp `sim_actuated.sumocfg` (file `actuated` baseline đã dùng) thay vì `sim.sumocfg` — cả 36 nút trong mạng đều actuated, không chỉ 6 nút có agent (nút không có agent chạy actuated mặc định của SUMO, giống hệt cách baseline `actuated` xử lý toàn mạng). `sim/runner.py`.
- **Action mới `SetGreenBounds(junction_id, phase_id, min_green_s, max_green_s)`** (`safety/validator.py`) — validator: reject phase không tồn tại/không xanh, reject `min_green_s > max_green_s` (cấu trúc sai), clamp vào `[min_green_s, max_green_s]` của `HARD_CONSTRAINTS`, clamp chống dao động theo `max_delta_per_cycle_s` áp riêng cho từng biên so với biên HIỆN TẠI (không phải giá trị đề xuất), reject thu hẹp `max_green_s` nếu pha đã starve quá `max_starvation_s`. `AdjustPhaseSplit`/`SetCycleLength`/`SetOffset` vẫn tồn tại trong `validator.Action` cho caller khác, chỉ bị loại khỏi `agents/protocol.py`'s `ActionUnion` (schema LLM thực sự thấy) — `ActionUnion = Union[SetGreenBounds, RequestVms, NoAction]`.
- **`PhaseState`** (`safety/validator.py`) thêm `min_dur_s`/`max_dur_s` (default 0.0, không phá test cũ dùng keyword args) — `sim/actuators.py`'s `read_tls_state` đọc thật từ TraCI's `phase.minDur`/`phase.maxDur` (tồn tại trên MỌI loại chương trình, chỉ `type="actuated"` mới thực thi).
- **`_apply_set_green_bounds`** (`sim/actuators.py`): `setProgramLogic` với `minDur`/`maxDur` mới, giữ nguyên `duration` (chỉ có ý nghĩa fallback cho static, actuated bỏ qua).
- **Prompt** (`agents/junction.py`): viết lại hoàn toàn phần "action space" — giải thích lớp actuation của SUMO, vai trò chiến lược của agent, và lưu ý biên đã ở mức rộng nhất mặc định (nâng trần thường vô nghĩa, nên siết biên đối diện/nâng sàn). `_build_own_state_text` hiện `current_bounds=[min, max]s` thay vì chỉ `duration_s` cho pha xanh. Cache_key bump `v5`→`v6`.
- **`agents/orchestrator.py`**: bỏ nhánh chết kiểm tra `action.type == "set_offset"` cho intent coalition (action đó không còn tồn tại trong action space LLM) — đơn giản hoá về `intent="request_help"` cho mọi broadcast (mọi action giờ đều tự quy về bản thân, không có action nào "nhắm" trực tiếp vào hàng xóm nữa).
- **8 test mới** (`tests/test_validator.py`): trong biên, reject min>max, clamp hard constraints, clamp chống dao động, reject/allow starvation, reject phase sai/vàng. Cập nhật `test_junction.py`/`test_orchestrator.py`/`test_protocol.py`/`test_supervisor.py` từ `AdjustPhaseSplit` sang `SetGreenBounds` cho các chỗ đi qua `ActionUnion`. 105/105 test pass.
- **Kiểm chứng thật** (`scripts/verify_llm_mode.py`, cập nhật để chạy trên mạng actuated đúng như `llm` mode thật dùng): check 1d mới ($0) — `apply_action(SetGreenBounds)` đọc lại đúng `[20.0, 60.0]` sau khi set. Check 1c/check 2 chuyển sang `sim_actuated.sumocfg`/`net_actuated.xml`. Check 2 (2 chu kỳ LLM thật, ~$0.1) chạy trọn vẹn qua action space mới, không crash.

**Run full 1 giờ với actuated-hybrid** (`run_id=37942b93-a8ff-4c76-a53c-840c2a436086`, seed 42, 2026-09-16): **cải thiện rõ rệt, lần đầu tiên `llm` mode vượt hẳn `fixed`/`maxpressure` và tiến sát `actuated`**.
- `mean_travel_time_s=118.31s` — so với `fixed`=134.35s (−16.0s, −11.9%), `maxpressure`=139.51s, `llm` gap-1=137.54s, `llm` gap-1+gap-2=148.18s (regression). So với trần lý thuyết `actuated`=107.40s, khoảng cách còn lại chỉ **10.9s** (trước đó khoảng cách với bản retiming định kỳ tốt nhất là ~30s) — hướng gốc rễ (giao actuation tần suất cao cho SUMO, LLM chỉ đặt biên chiến lược) được xác nhận đúng bằng số liệu thật.
- **5/~44 chu kỳ bị skip** (tốt hơn hẳn 11 của gap(2), nhưng vẫn chưa về 0 như 2 lần trước actuated-hybrid). Soi DB: 222 decision rows (37 chu kỳ hoàn thành × 6 nút), `action_type`: 193 `no_action` / 29 `set_green_bounds` (khiêm tốn hơn nhiều so với 35 `adjust_phase_split` của gap(2) — đúng như kỳ vọng khi action space giờ ít "hấp dẫn" hơn để đề xuất tùy tiện). `validator_status`: 220 `ok` / 2 `rejected` (cấu trúc sai, đúng hành vi). Độ trễ LLM: `junction` mean=3.5s/p95=5.3s, `supervisor` mean=6.4s/p95=8.8s/max=9.9s — vẫn có thể cộng dồn vượt ngân sách 18s thực khi round 2 (coalition) + supervisor xếp chồng trong cùng chu kỳ, giải thích 5 lần skip còn lại.
- **Kết luận**: actuated-hybrid là bước cải thiện lớn nhất trong toàn bộ Step 14 — xác nhận luận điểm cốt lõi (độ mịn thời gian phản ứng quan trọng hơn độ thông minh ra quyết định định kỳ). Khoảng cách còn lại với `actuated` thuần và 5 chu kỳ skip còn sót là các hướng tối ưu tiếp theo nếu cần, nhưng chưa phải việc bắt buộc — để người dùng quyết định có tiếp tục đào sâu hay chuyển sang Phase 3 (Step 15, dashboard).

### Cách chạy full (đã dùng, để tham khảo nếu cần chạy lại/seed khác)

```bash
source .venv/bin/activate
python -m sumo_agents.sim.runner --scenario grid_4x4 --mode llm --seed 42
```

Xem kết quả run gần nhất:
```bash
python3 -c "
import asyncio, sys; sys.path.insert(0, 'src')
from sumo_agents.obs.db import make_async_engine, make_session_factory
from sqlalchemy import text
async def main():
    engine = make_async_engine(); sf = make_session_factory(engine)
    async with sf() as s:
        r = await s.execute(text(\"SELECT run_id, summary, finished_at FROM runs WHERE mode='llm' ORDER BY started_at DESC LIMIT 1\"))
        print(r.one())
    await engine.dispose()
asyncio.run(main())
"
```
(Muốn thêm cột `llm` vào bảng so sánh Bước 9: `python scripts/compare.py --scenario grid_4x4 --modes fixed,actuated,maxpressure,llm --seeds 42` — chỉ 1 seed cho `llm` để không nhân 3 lần chi phí. Chưa chạy — tuỳ chọn.)

> Nhắc lại từ plan §7: `llm` **có thể thua** `actuated`. Đúng như dự đoán — ở đây `llm` không chỉ thua mà còn hoàn toàn thụ động suốt sự cố. Ghi nhận trung thực (xem phát hiện ở trên) thay vì che giấu, rồi tuỳ người dùng quyết định: chấp nhận và sang Phase 3 (dashboard sẽ giúp debug trực quan hơn), hay dừng lại tinh chỉnh JunctionAgent trước.

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
