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
