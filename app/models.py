"""Pydantic request/response models and OpenAPI examples."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CODE_PATTERN = r"^[A-Za-z0-9_.\-]+$"
CODE_EXAMPLE = "T1"


class AreaCreate(BaseModel):
    code: str = Field(
        min_length=1,
        max_length=64,
        pattern=CODE_PATTERN,
        description="探方/分区的稳定编码",
        examples=["T1"],
    )
    name: str = Field(min_length=1, max_length=200, examples=["北区 T1 探方"])

    model_config = ConfigDict(extra="forbid")


class LayerCreate(BaseModel):
    code: str = Field(
        min_length=1,
        max_length=64,
        pattern=CODE_PATTERN,
        description="分区内唯一的地层编号",
        examples=["L3"],
    )
    name: str = Field(min_length=1, max_length=200, examples=["战国文化层"])
    description: str | None = Field(default=None, max_length=2000)

    model_config = ConfigDict(extra="forbid")


class AdditionIn(BaseModel):
    record_number: str = Field(
        min_length=1,
        max_length=128,
        description="新地层关系记录号；分区内唯一，撤回后也不可复用",
        examples=["R-T1-101"],
    )
    earlier_layer_id: int = Field(gt=0, description="较早地层的全局 ID")
    later_layer_id: int = Field(gt=0, description="较晚地层的全局 ID")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _distinct_endpoints(self) -> "AdditionIn":
        if self.earlier_layer_id == self.later_layer_id:
            raise ValueError("较早地层与较晚地层不能相同")
        return self


class WithdrawalIn(BaseModel):
    record_number: str = Field(
        min_length=1,
        max_length=128,
        description="本分区已生效记录号",
        examples=["R-T1-004"],
    )
    reason: str | None = Field(default=None, max_length=2000)

    model_config = ConfigDict(extra="forbid")


class BatchCreate(BaseModel):
    base_version: int = Field(
        ge=0,
        description="提交者看到的该分区版本号；与当前版本不符时整单退回",
        examples=[1],
    )
    additions: list[AdditionIn] = Field(default_factory=list)
    withdrawals: list[WithdrawalIn] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=2000)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _non_empty(self) -> "BatchCreate":
        if not self.additions and not self.withdrawals:
            raise ValueError("批次必须至少包含一条补录或撤回")
        return self


class LayerRef(BaseModel):
    id: int
    area: str
    code: str
    name: str


class RecordOut(BaseModel):
    record_number: str
    owner_area: str
    earlier: LayerRef
    later: LayerRef
    active: bool
    created_at: str
    withdrawn_at: str | None = None
    withdrawal_reason: str | None = None
    created_by_batch_id: int | None = None
    withdrawn_by_batch_id: int | None = None


class BatchOut(BaseModel):
    batch_id: int
    area_code: str
    base_version: int
    area_version: int
    site_version: int
    note: str | None
    created_at: str
    additions: list[RecordOut]
    withdrawals: list[RecordOut]
    effective_relation_count: int = Field(
        description="批次生效后全区的不同早晚关系对数（同一对可有多条记录）"
    )


class AreaOut(BaseModel):
    id: int
    code: str
    name: str
    version: int
    layer_count: int
    active_record_count: int
    created_at: str


class LayerOut(BaseModel):
    id: int
    area: str
    code: str
    name: str
    description: str | None
    created_at: str


class RankedLayer(LayerRef):
    rank: int


class RankOut(BaseModel):
    layer: LayerRef
    site_version: int
    area_versions: dict[str, int]
    total_layers: int
    earliest_rank: int = Field(description="在所有合规全区排列中的最小名次（1 起）")
    latest_rank: int = Field(description="在所有合规全区排列中的最大名次（1 起）")
    ancestor_count: int = Field(description="必须排在该层之前的地层数")
    descendant_count: int = Field(description="必须排在该层之后的地层数")
    earliest_order: list[RankedLayer] = Field(description="取得最早名次的一份完整可行排列")
    latest_order: list[RankedLayer] = Field(description="取得最晚名次的一份完整可行排列")
    method: Literal["reachability"] = "reachability"


class CycleEdgeOut(BaseModel):
    earlier: LayerRef
    later: LayerRef
    record_numbers: list[str]


class CycleOut(BaseModel):
    layers: list[LayerRef] = Field(description="环上的地层，首尾由最后一条边闭合")
    edges: list[CycleEdgeOut]
    record_numbers: list[str] = Field(description="构成该环的全部生效记录号")


class BatchListItem(BaseModel):
    batch_id: int
    base_version: int
    area_version: int
    site_version: int
    additions_count: int
    withdrawals_count: int
    note: str | None
    created_at: str


class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"
    site_version: int


class LayerCreated(LayerOut):
    area_version: int = Field(description="写入后该分区的新版本号")


class AreaCreated(AreaOut):
    site_version: int = Field(description="写入后的全区已发布版本号")


class LayerIdItem(BaseModel):
    code: str
    id: int


class ConflictDemoOut(BaseModel):
    """Copy-paste payloads for the two-trench conflict walkthrough."""

    instructions: str
    first_batch: dict[str, Any] = Field(description="{area, body} 先提交的一单")
    second_batch: dict[str, Any] = Field(description="{area, body} 后提交、应被退回的一单")
    expected: str


class DemoSeedOut(BaseModel):
    seeded: bool = Field(description="本次是否实际写入；false 表示已存在演示区")
    areas: list[AreaOut]
    layers: dict[str, list[LayerIdItem]] = Field(
        description="每个演示区的 {code, id} 列表"
    )
    versions: dict[str, int]
    demo_rank_layer: dict[str, Any] | None
    conflict_demo: ConflictDemoOut | None = Field(
        default=None,
        description="两探方相互冲突批次的现成请求体（T1 的 L1 与 T2 的 M1 互指）",
    )
    suggested_steps: list[str]


def error_example(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"detail": {"code": code, "message": message, **extra}}
