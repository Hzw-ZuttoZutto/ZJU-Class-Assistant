from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path


class SimulatorMode(IntEnum):
    MODE1 = 1
    MODE2 = 2
    MODE3 = 3
    MODE4 = 4
    MODE5 = 5

    @classmethod
    def from_int(cls, value: int) -> "SimulatorMode":
        return cls(int(value))


ALLOWED_CONTROL_STATUS = {"ok", "timeout", "error", "drop"}
ALLOWED_MODE5_PROFILES = {"all_chunks_dual", "single_chunk_dual", "all_chunks_serial_once"}
DEFAULT_MODE5_PROFILE = "all_chunks_dual"


@dataclass
class DatasetConfig:
    files: list[str] = field(default_factory=list)
    include_glob: str = "*.mp3"


@dataclass
class FeedDuplicateRule:
    seq: int
    times: int = 1


@dataclass
class FeedDelayBackfillRule:
    seq: int
    delay_sec: float


@dataclass
class FeedBarrierRule:
    after_seq: int
    pause_sec: float


@dataclass
class FeedConfig:
    mode: str = "realtime"
    speed: float = 1.0
    jitter_max_sec: float = 0.0
    drop: list[int] = field(default_factory=list)
    duplicate: list[FeedDuplicateRule] = field(default_factory=list)
    reorder: list[int] = field(default_factory=list)
    delay_backfill: list[FeedDelayBackfillRule] = field(default_factory=list)
    barriers: list[FeedBarrierRule] = field(default_factory=list)


@dataclass
class StageControlRule:
    seq: int
    status: str = "ok"
    delay_sec: float = 0.0
    forced_text: str = ""
    forced_result: dict = field(default_factory=dict)

    def normalized_status(self) -> str:
        text = (self.status or "ok").strip().lower()
        return text if text in ALLOWED_CONTROL_STATUS else "ok"


@dataclass
class HistoryRule:
    seq: int
    visibility: str
    hold_sec: float = 0.0


@dataclass
class PrecomputeConfig:
    workers: int = 4


@dataclass
class BenchmarkConfig:
    parallel_workers: int = 4
    repeats: int = 1


@dataclass
class Scenario:
    mode: SimulatorMode
    name: str
    dataset: DatasetConfig = field(default_factory=DatasetConfig)
    feed: FeedConfig = field(default_factory=FeedConfig)
    translation_rules: list[StageControlRule] = field(default_factory=list)
    analysis_rules: list[StageControlRule] = field(default_factory=list)
    history_rules: list[HistoryRule] = field(default_factory=list)
    precompute: PrecomputeConfig = field(default_factory=PrecomputeConfig)
    benchmark: BenchmarkConfig = field(default_factory=BenchmarkConfig)
    seed: int | None = None
    mode3_variant: str = "complete_history"

    _translation_rule_by_seq: dict[int, StageControlRule] = field(init=False, default_factory=dict)
    _analysis_rule_by_seq: dict[int, StageControlRule] = field(init=False, default_factory=dict)
    _history_rule_by_seq: dict[int, HistoryRule] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._translation_rule_by_seq = {rule.seq: rule for rule in self.translation_rules}
        self._analysis_rule_by_seq = {rule.seq: rule for rule in self.analysis_rules}
        self._history_rule_by_seq = {rule.seq: rule for rule in self.history_rules}

    def translation_rule_for(self, seq: int) -> StageControlRule | None:
        return self._translation_rule_by_seq.get(seq)

    def analysis_rule_for(self, seq: int) -> StageControlRule | None:
        return self._analysis_rule_by_seq.get(seq)

    def history_rule_for(self, seq: int) -> HistoryRule | None:
        return self._history_rule_by_seq.get(seq)


@dataclass
class SimulateRuntimeConfig:
    mode: SimulatorMode
    scenario_file: Path
    sim_root: Path
    mp3_dir: Path
    run_dir: Path
    chunk_seconds: int
    precompute_workers: int
    rt_model: str
    rt_stt_model: str
    rt_keywords_file: Path
    rt_api_base_url: str
    rt_request_timeout_sec: float
    rt_stage_timeout_sec: float
    rt_retry_count: int
    seed: int | None
    mode5_profile: str = DEFAULT_MODE5_PROFILE
    mode5_target_seq: int | None = None
