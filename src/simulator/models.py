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
    MODE6 = 6

    @classmethod
    def from_int(cls, value: int) -> "SimulatorMode":
        return cls(int(value))


ALLOWED_CONTROL_STATUS = {"ok", "timeout", "error", "drop"}
ALLOWED_MODE5_PROFILES = {"all_chunks_dual", "single_chunk_dual", "all_chunks_serial_once"}
DEFAULT_MODE5_PROFILE = "all_chunks_dual"
ALLOWED_MODE6_STT_STEP_TYPES = {"ok", "error", "timeout_request"}
ALLOWED_MODE6_ANALYSIS_STEP_TYPES = {"ok", "error", "timeout_request"}
ALLOWED_MODE6_CONTEXT_REASONS = {"full18_ready", "timeout_wait_full18", "timeout_wait_recent4"}
ALLOWED_MODE6_ANALYSIS_STATUSES = {"ok", "analysis_drop_timeout", "analysis_drop_error"}


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
class Mode6CaseConfig:
    request_timeout_sec: float | None = None
    stage_timeout_sec: float | None = None
    retry_count: int | None = None
    context_recent_required: int | None = None
    context_target_chunks: int | None = None
    context_wait_timeout_sec_1: float | None = None
    context_wait_timeout_sec_2: float | None = None


@dataclass
class Mode6SttStep:
    type: str = "ok"
    text: str = ""
    error: str = ""
    delay_sec: float = 0.0

    def normalized_type(self) -> str:
        text = (self.type or "ok").strip().lower()
        return text if text in ALLOWED_MODE6_STT_STEP_TYPES else "ok"


@dataclass
class Mode6AnalysisStep:
    type: str = "ok"
    error: str = ""
    delay_sec: float = 0.0
    result: dict = field(default_factory=dict)

    def normalized_type(self) -> str:
        text = (self.type or "ok").strip().lower()
        return text if text in ALLOWED_MODE6_ANALYSIS_STEP_TYPES else "ok"


@dataclass
class Mode6HistoryItem:
    seq: int
    text: str


@dataclass
class Mode6HistoryArrival:
    at_sec: float
    seq: int
    text: str


@dataclass
class Mode6Expected:
    stt_status: str = ""
    stt_attempts: int | None = None
    analysis_called: bool | None = None
    analysis_status: str = ""
    analysis_attempts: int | None = None
    analysis_elapsed_sec_lte: float | None = None
    context_reason: str = ""
    context_chunk_count: int | None = None
    missing_ranges: list[str] | None = None


def _default_mode6_analysis_script() -> list[Mode6AnalysisStep]:
    return [Mode6AnalysisStep(type="ok")]


@dataclass
class Mode6Case:
    id: str
    chunk_seq: int
    config: Mode6CaseConfig = field(default_factory=Mode6CaseConfig)
    stt_script: list[Mode6SttStep] = field(default_factory=list)
    analysis_script: list[Mode6AnalysisStep] = field(default_factory=_default_mode6_analysis_script)
    history_initial: list[Mode6HistoryItem] = field(default_factory=list)
    history_arrivals: list[Mode6HistoryArrival] = field(default_factory=list)
    expected: Mode6Expected = field(default_factory=Mode6Expected)


@dataclass
class Mode6Config:
    check_interval_sec: float = 0.2
    cases: list[Mode6Case] = field(default_factory=list)


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
    mode6: Mode6Config = field(default_factory=Mode6Config)
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
