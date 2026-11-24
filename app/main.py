"""FastAPI application exposing the stratigraphic rank service.

Interactive documentation is served at ``/docs``.  A suggested walkthrough is
in the OpenAPI description and also returned by ``POST /demo/seed``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import demo, service
from .config import Settings
from .db import init_db
from .errors import ApiError
from .models import (
    AreaCreate,
    AreaCreated,
    AreaOut,
    BatchCreate,
    BatchListItem,
    BatchOut,
    DemoSeedOut,
    HealthOut,
    LayerCreate,
    LayerCreated,
    LayerOut,
    RankOut,
    RecordOut,
    error_example,
)

DESCRIPTION = """
藏品档案中的**地层清单**与**带记录号的早晚关系**分开保存。合并各探方后，
每一次合规的全区排列都是一个拓扑序；本服务据此核定指定地层能取到的最早、
最晚名次，并各附一份可行排列。

### 名次算法
名次无需枚举排列。设地层 `v` 的祖先集合为 `A(v)`（经任意条早晚关系可到达
`v` 的地层）、后代集合为 `D(v)`：

* 最早名次 = `1 + |A(v)|`
* 最晚名次 = `n - |D(v)|`

再对任一拓扑序做一次过滤，即得到取得该名次的一份完整可行排列。

### 批次语义
* 馆员提交时携带 `base_version`（所见该分区版本号）。若同区已有别的整单
  先生效，版本不符，**整单退回**，有效关系不变。
* 补录与撤回都以**整单**为原子单位；校验失败、成环或写入失败均不改变任何
  已发布关系。
* 同一对地层的早晚关系可有多条记录；只要还有一条生效，该关系就仍然成立，
  撤回全部记录后才解除。
* 提交与核定期间，其他分区照常可提交；查询始终读最近一次已发布的整版关系
  （SQLite WAL 快照），不会看到未提交的中间状态。
* 若整单生效会形成环，返回 `409`，列出环上的地层与对应记录号。

### /docs 演示：撤证后重查名次
1. `POST /demo/seed` 建立演示数据（T1/T2 两个探方）。响应中给出 `M2` 的
   `layer_id`、各区当前 `version`，以及建议步骤。
2. `GET /ranks/{layer_id}` 查看 `M2`：`earliest_rank=2`、`latest_rank=5`。
3. 用 T2 的 `version` 调 `POST /areas/T2/batches`，只撤回 `R-T2-002`。
   重新查询名次**不变**——`R-T2-001` 仍然记录着同一对早晚关系。
4. 再次提交 T2 批次撤回 `R-T2-001`；重查 `M2` 的 `earliest_rank` 变为 `1`。
5. 用 T1 的版本调 `POST /areas/T1/batches` 撤回 `R-T1-004`；重查
   `latest_rank` 变为 `6`。
"""

TAGS_METADATA = [
    {"name": "概览", "description": "健康检查与演示数据"},
    {"name": "分区", "description": "探方（分区）的登记与查询"},
    {"name": "地层", "description": "分区内的地层清单"},
    {"name": "记录", "description": "带记录号的早晚关系；可含跨探方关系"},
    {"name": "批次", "description": "馆员按所见版号整单补录或撤回"},
    {"name": "核定", "description": "查询指定地层的首末名次与可行排列"},
]

logger = logging.getLogger("app.main")


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(config.db_path, config.busy_timeout_ms)
        if config.seed_demo:
            try:
                demo.seed_demo(config.db_path, config.busy_timeout_ms)
            except ApiError:
                # Never prevent the service from starting because the demo
                # inventory could not be written; /demo/seed reports details.
                logger.exception("demo seed skipped")
        yield

    app = FastAPI(
        title=config.title,
        version=config.version,
        description=DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
    )
    app.state.settings = config

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail()})

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # ``exc.errors()`` may carry exception objects in ``ctx``; keep the
        # response JSON-serialisable while preserving the useful details.
        errors = []
        for error in exc.errors():
            ctx = {
                key: str(value) if isinstance(value, BaseException) else value
                for key, value in (error.get("ctx") or {}).items()
            }
            errors.append({**error, "ctx": ctx})
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "request_validation_error",
                    "message": "请求格式不合法，整单未提交",
                    "errors": errors,
                }
            },
        )

    # ------------------------------------------------------------------
    # Health and demo
    # ------------------------------------------------------------------

    @app.get("/health", response_model=HealthOut, tags=["概览"])
    def get_health() -> dict[str, Any]:
        """Return liveness and the currently published site version."""

        return service.health(config.db_path, config.busy_timeout_ms)

    @app.post("/demo/seed", response_model=DemoSeedOut, tags=["概览"])
    def seed_demo() -> dict[str, Any]:
        """Create (or return) the T1/T2 demonstration inventory.

        Safe to call repeatedly; it never duplicates or resets data.  The
        response gives the layer id to query and the current area versions to
        use as ``base_version`` in the /docs walkthrough.
        """

        return demo.seed_demo(config.db_path, config.busy_timeout_ms)

    # ------------------------------------------------------------------
    # Areas
    # ------------------------------------------------------------------

    @app.post(
        "/areas",
        response_model=AreaCreated,
        status_code=201,
        tags=["分区"],
        responses={409: {"description": "分区编码重复"}},
    )
    def create_area(payload: AreaCreate) -> dict[str, Any]:
        """Register a new trench/analytical area (探方/分区)."""

        return service.create_area(
            config.db_path,
            code=payload.code,
            name=payload.name,
            busy_timeout_ms=config.busy_timeout_ms,
        )

    @app.get("/areas", response_model=list[AreaOut], tags=["分区"])
    def list_areas() -> list[dict[str, Any]]:
        """List areas with their current version and counts."""

        return service.list_areas(config.db_path, config.busy_timeout_ms)

    @app.get(
        "/areas/{area_code}",
        response_model=AreaOut,
        tags=["分区"],
        responses={404: {"description": "分区不存在"}},
    )
    def get_area(area_code: str) -> dict[str, Any]:
        """Fetch one area, including the version needed for batch submission."""

        return service.get_area(config.db_path, area_code, config.busy_timeout_ms)

    @app.post(
        "/areas/{area_code}/layers",
        response_model=LayerCreated,
        status_code=201,
        tags=["地层"],
        responses={404: {"description": "分区不存在"}, 409: {"description": "地层编号重复"}},
    )
    def create_layer(area_code: str, payload: LayerCreate) -> dict[str, Any]:
        """Add a layer to an area's inventory.

        This bumps the area version, so in-flight batches based on an older
        version are rejected wholesale rather than applied to a stale plan.
        """

        return service.create_layer(
            config.db_path,
            area_code,
            code=payload.code,
            name=payload.name,
            description=payload.description,
            busy_timeout_ms=config.busy_timeout_ms,
        )

    @app.get(
        "/areas/{area_code}/layers",
        response_model=list[LayerOut],
        tags=["地层"],
        responses={404: {"description": "分区不存在"}},
    )
    def list_layers(area_code: str) -> list[dict[str, Any]]:
        """List the layers recorded for one area."""

        return service.list_layers(config.db_path, area_code, config.busy_timeout_ms)

    @app.get(
        "/layers/{layer_id}",
        response_model=LayerOut,
        tags=["地层"],
        responses={404: {"description": "地层不存在"}},
    )
    def get_layer(layer_id: int) -> dict[str, Any]:
        """Fetch a layer by its global id."""

        return service.get_layer(config.db_path, layer_id, config.busy_timeout_ms)

    # ------------------------------------------------------------------
    # Records
    # ------------------------------------------------------------------

    @app.get(
        "/areas/{area_code}/records",
        response_model=list[RecordOut],
        tags=["记录"],
        responses={404: {"description": "分区不存在"}},
    )
    def list_records(
        area_code: str,
        include_withdrawn: bool = Query(
            default=False, description="是否包含已撤回记录（审计用）"
        ),
    ) -> list[dict[str, Any]]:
        """List relation records owned by an area.

        Endpoints may belong to other areas: records may merge trench
        sequences, and the record number is issued by the owning area.
        """

        return service.list_records(
            config.db_path,
            area_code,
            include_withdrawn=include_withdrawn,
            busy_timeout_ms=config.busy_timeout_ms,
        )

    @app.get("/relations", tags=["记录"])
    def list_relations() -> dict[str, Any]:
        """Show the merged, currently published site-wide relation groups.

        Each group lists every active record number that states the same
        ordered layer pair, which makes the "all records must be withdrawn"
        rule visible.
        """

        return service.site_relations(config.db_path, config.busy_timeout_ms)

    # ------------------------------------------------------------------
    # Batches
    # ------------------------------------------------------------------

    @app.post(
        "/areas/{area_code}/batches",
        response_model=BatchOut,
        status_code=201,
        tags=["批次"],
        responses={
            404: {"description": "分区不存在"},
            409: {
                "description": "整单退回：版号过期、校验失败或成环",
                "content": {
                    "application/json": {
                        "examples": {
                            "versionConflict": {
                                "value": error_example(
                                    "area_version_conflict",
                                    "分区版本已变更，整单退回；请重新读取后提交",
                                    base_version=1,
                                    current_version=2,
                                )
                            },
                            "cycleDetected": {
                                "value": error_example(
                                    "cycle_detected",
                                    "批次生效后会形成地层先后环，整单退回",
                                    cycle={
                                        "layers": [],
                                        "edges": [],
                                        "record_numbers": ["R-T1-004"],
                                    },
                                )
                            },
                        }
                    }
                },
            },
            503: {"description": "写锁等待超时，整单未生效"},
        },
    )
    def submit_batch(area_code: str, payload: BatchCreate) -> dict[str, Any]:
        """Publish a whole batch of additions and withdrawals atomically.

        The batch is rejected wholesale when:

        * ``base_version`` is not the area's current version (another batch or
          inventory change was published first),
        * a record number is unknown, already withdrawn, or reused,
        * a referenced layer does not exist,
        * applying it would create a cycle across the merged site graph.

        On any failure the previously published relations remain untouched.
        """

        model = payload.model_dump()
        return service.submit_batch(
            config.db_path,
            area_code,
            base_version=model["base_version"],
            additions=model["additions"],
            withdrawals=model["withdrawals"],
            note=model["note"],
            busy_timeout_ms=config.busy_timeout_ms,
        )

    @app.get(
        "/areas/{area_code}/batches",
        response_model=list[BatchListItem],
        tags=["批次"],
        responses={404: {"description": "分区不存在"}},
    )
    def list_batches(area_code: str) -> list[dict[str, Any]]:
        """List published batches for an area in publication order."""

        return service.list_batches(config.db_path, area_code, config.busy_timeout_ms)

    @app.get(
        "/batches/{batch_id}",
        response_model=BatchOut,
        tags=["批次"],
        responses={404: {"description": "批次不存在"}},
    )
    def get_batch(batch_id: int) -> dict[str, Any]:
        """Fetch a batch with the records it added and withdrew."""

        return service.get_batch(config.db_path, batch_id, config.busy_timeout_ms)

    # ------------------------------------------------------------------
    # Rank query
    # ------------------------------------------------------------------

    @app.get(
        "/ranks/{layer_id}",
        response_model=RankOut,
        tags=["核定"],
        responses={
            404: {"description": "地层不存在"},
            409: {"description": "已发布关系中存在环"},
        },
    )
    def get_rank(layer_id: int) -> dict[str, Any]:
        """Compute the earliest/latest possible rank of a layer.

        Uses ancestor/descendant counts on the currently published graph, so it
        runs in O(V + E) and never enumerates permutations.  Each bound comes
        with a complete valid site-wide ordering that attains it.
        """

        return service.get_rank(config.db_path, layer_id, config.busy_timeout_ms)

    return app


app = create_app()
