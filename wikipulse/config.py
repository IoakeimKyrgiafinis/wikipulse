"""All WikiPulse settings, in one place.

Values come from environment variables first, then from a `.env` file in the working
directory, then from the defaults below. Field names map to environment variables
case-insensitively, so `redis_url` is set by `REDIS_URL`.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Text that marks a User-Agent the human has not filled in yet.
USER_AGENT_PLACEHOLDER_MARKERS: tuple[str, ...] = ("<you>", "<your email>", "example.com")

DEFAULT_USER_AGENT = "WikiPulse/0.1 (https://github.com/<you>/wikipulse; <your email>)"


class DetectorConfig(BaseModel):
    """Every threshold of the spike detector (stage 2), in one immutable object.

    Built from the `DETECT_*` settings by `Settings.detector_config()`; tests build it
    directly. See docs/learning/02a-spike-detector.md for why each value was chosen.
    """

    model_config = ConfigDict(frozen=True)

    # Sliding windows: the short one is the "now" signal, the long one delays the baseline.
    short_window_min: float = Field(default=10, gt=0)
    long_window_min: float = Field(default=60, gt=0)
    # A spike starts when, in the short window, a page has at least `min_edits` human
    # edits from at least `min_editors` different people, and at least `min_ratio` times
    # the edits its baseline expects.
    min_edits: int = Field(default=5, ge=1)
    min_editors: int = Field(default=3, ge=1)
    min_ratio: float = Field(default=5.0, gt=0)
    # Second start rule, for stories that build up over an hour rather than 10 minutes:
    # in the long window, at least `long_min_edits` edits from `long_min_editors` people
    # (and `min_ratio` times the expected count for the long window).
    long_min_edits: int = Field(default=10, ge=1)
    long_min_editors: int = Field(default=4, ge=1)
    # Lowest expected count per short window (scaled up for the long window) used in the
    # score, so a page without history doesn't get an infinite score.
    expected_floor: float = Field(default=0.5, gt=0)
    # A running spike stays "hot" while the short window holds at least this many edits;
    # it ends after `quiet_period_min` minutes without a hot moment.
    continue_min_edits: int = Field(default=2, ge=1)
    quiet_period_min: float = Field(default=30, gt=0)
    # Baseline: exponentially decaying average with this time constant (hours).
    baseline_tau_hours: float = Field(default=24, gt=0)
    # Pages whose baseline falls below this many edits per day are forgotten.
    baseline_prune_per_day: float = Field(default=0.05, ge=0)
    # Daily rhythm: overall edit rate over the last `rhythm_tau_hours` compared with the
    # baseline time constant, clamped to [rhythm_min, rhythm_max].
    rhythm_tau_hours: float = Field(default=1, gt=0)
    rhythm_min: float = Field(default=0.5, gt=0)
    rhythm_max: float = Field(default=2.0, gt=0)
    # Count reverts ("Reverted ...", "Undid ...") as normal edits? Default: no.
    count_reverts: bool = False


class EventsConfig(BaseModel):
    """How spikes are grouped into events (stage 3, spec 03b).

    Built from the `EVENTS_*` settings by `Settings.events_config()`; tests build it
    directly. See docs/learning/03b-event-grouping.md for how the values were chosen.
    """

    model_config = ConfigDict(frozen=True)

    # Weights of the three signals in the pairwise score (normalized by their sum).
    link_weight: float = Field(default=0.4, ge=0)
    time_weight: float = Field(default=0.2, ge=0)
    embed_weight: float = Field(default=0.4, ge=0)
    # Time signal: 1 when two spikes start together, falling linearly to 0 when their
    # starts are this many hours apart.
    time_window_h: float = Field(default=6, gt=0)
    # Embedding signal: cosine similarity of the summaries, rescaled so that `embed_floor`
    # (what unrelated summaries typically reach) gives 0 and 1.0 gives 1.
    embed_floor: float = Field(default=0.5, ge=0, lt=1)
    # A new spike joins an active event when its score with one of the event's pages
    # reaches this threshold.
    join_threshold: float = Field(default=0.55, gt=0, le=1)
    # A spike's link to a page counts fully only if the linking page has at most this
    # many links; long list pages ("Deaths in 2026") link to everything, so a link from
    # them counts proportionally less. 0 switches this off.
    link_hub_size: int = Field(default=1000, ge=0)
    # A link counts only if the two summaries are at least this similar (cosine). A link
    # between pages whose summaries look unrelated is usually a list or navigation link
    # ("Deaths in 2026" -> a conductor), not a shared story; without this rule, link plus
    # timing alone (0.4 + 0.2) reaches the join threshold and chains unrelated pages
    # through such a list. 0 switches this off. Not applied when an embedding is missing.
    link_min_cosine: float = Field(default=0.5, ge=0, le=1)
    # A spike counts as over this many minutes after its last edit (the detector's quiet
    # period), so a replay closes events exactly when the live detector would.
    spike_close_min: float = Field(default=30, gt=0)


class Settings(BaseSettings):
    """Application settings loaded from the environment and `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env also holds Docker Compose variables we don't use here
    )

    redis_url: str = "redis://localhost:6379/0"
    postgres_dsn: str = "postgresql://wikipulse:wikipulse@localhost:5432/wikipulse"
    # Deliberately not a working value: Wikimedia requires a real contact address.
    user_agent: str = DEFAULT_USER_AGENT
    log_level: LogLevel = "INFO"
    data_dir: Path = Path("data")

    # --- Stage 1: live stream consumer ---
    # Wikimedia EventStreams endpoint for all recent changes (Server-Sent Events).
    stream_url: str = "https://stream.wikimedia.org/v2/stream/recentchange"
    # Redis Stream that receives the filtered, normalized edits.
    stream_key: str = "wiki:edits"
    # Redis Stream that `replay` writes to by default. Kept apart from STREAM_KEY so a
    # replay never puts duplicate events into the live stream.
    replay_stream_key: str = "wiki:replay:edits"
    # Approximate upper bound on the Redis Stream length (XADD MAXLEN ~).
    stream_maxlen: int = Field(default=1_000_000, gt=0)
    # Longest wait between reconnect attempts, in seconds.
    reconnect_max_s: float = Field(default=60.0, gt=0)

    # --- Stage 2: spike detection (thresholds explained in DetectorConfig) ---
    detect_short_window_min: float = Field(default=10, gt=0)
    detect_long_window_min: float = Field(default=60, gt=0)
    detect_min_edits: int = Field(default=5, ge=1)
    detect_min_editors: int = Field(default=3, ge=1)
    detect_min_ratio: float = Field(default=5.0, gt=0)
    detect_long_min_edits: int = Field(default=10, ge=1)
    detect_long_min_editors: int = Field(default=4, ge=1)
    detect_expected_floor: float = Field(default=0.5, gt=0)
    detect_continue_min_edits: int = Field(default=2, ge=1)
    detect_quiet_period_min: float = Field(default=30, gt=0)
    detect_baseline_tau_hours: float = Field(default=24, gt=0)
    detect_baseline_prune_per_day: float = Field(default=0.05, ge=0)
    detect_rhythm_tau_hours: float = Field(default=1, gt=0)
    detect_rhythm_min: float = Field(default=0.5, gt=0)
    detect_rhythm_max: float = Field(default=2.0, gt=0)
    detect_count_reverts: bool = False
    # How often (wall-clock seconds) the detector saves its state to Redis, writes
    # spikes to Postgres and acknowledges the stream entries it has processed. 30 s keeps
    # new spikes' enrichment (stage 3, which reads them from Postgres) within a minute.
    detect_snapshot_interval_s: float = Field(default=30, gt=0)

    # --- Stage 2: detection evaluation ---
    # MediaWiki Action API and REST API of English Wikipedia (ground truth, label context).
    wiki_api_url: str = "https://en.wikipedia.org/w/api.php"
    wiki_rest_url: str = "https://en.wikipedia.org/api/rest_v1"
    # Smallest pause between two requests to Wikimedia APIs, in seconds (be polite).
    wiki_api_min_interval_s: float = Field(default=1.0, ge=0)
    # Model that labels spikes as news / not news, as `backend:model` (see `wikipulse.llm`).
    eval_label_model: str = "ollama:qwen3:8b"

    # --- Stage 3: page enrichment ---
    # Most requests to Wikimedia APIs in flight at the same time (async enrichment client).
    wiki_api_concurrency: int = Field(default=4, ge=1)
    # Smallest pause between the *starts* of two enrichment requests, in seconds. With the
    # concurrency limit this caps the request rate (0.5 s: at most 2 requests per second;
    # 5 per second drew HTTP 429 "too many requests" from Wikimedia).
    enrich_min_interval_s: float = Field(default=0.5, ge=0)
    # How long cached API responses (and enriched pages) stay fresh, in hours.
    enrich_cache_ttl_h: float = Field(default=24, gt=0)
    # Keep at most this many outgoing article links per page (500 per request).
    enrich_max_links: int = Field(default=5000, ge=1)
    # How often the enrichment runner looks for new spikes in Postgres, in seconds.
    enrich_poll_interval_s: float = Field(default=10, gt=0)

    # --- Stage 3: event grouping (weights and thresholds explained in EventsConfig) ---
    events_link_weight: float = Field(default=0.4, ge=0)
    events_time_weight: float = Field(default=0.2, ge=0)
    events_embed_weight: float = Field(default=0.4, ge=0)
    events_time_window_h: float = Field(default=6, gt=0)
    events_embed_floor: float = Field(default=0.5, ge=0, lt=1)
    events_join_threshold: float = Field(default=0.55, gt=0, le=1)
    events_link_hub_size: int = Field(default=1000, ge=0)
    events_link_min_cosine: float = Field(default=0.5, ge=0, le=1)
    events_spike_close_min: float = Field(default=30, gt=0)
    # Sentence-transformers model that embeds page summaries (downloaded once, then cached
    # by Hugging Face on this machine).
    events_embed_model: str = "BAAI/bge-small-en-v1.5"
    # How often the grouper looks for new spikes, in seconds.
    events_poll_interval_s: float = Field(default=10, gt=0)
    # A live spike waits at most this long (wall clock) for its page to be enriched
    # before it is grouped without links and summary.
    events_enrich_wait_s: float = Field(default=120, ge=0)

    # --- Stage 4: LLM client (wikipulse.llm) ---
    # Model used when a caller doesn't name one, as `backend:model` (`ollama:qwen3:4b`,
    # `anthropic:claude-sonnet-4-5`, ...).
    default_model: str = "ollama:qwen3:4b"
    # Where the local Ollama server listens.
    ollama_host: str = "http://localhost:11434"
    # Context window (tokens) sent with every Ollama call. Ollama's own default is small
    # and it silently cuts off longer prompts, so it is always set explicitly.
    ollama_num_ctx: int = Field(default=8192, ge=512)
    # How long Ollama keeps a model in (GPU) memory after a call, e.g. "10m", "1h", "0".
    ollama_keep_alive: str = "10m"
    # Longest wait for one LLM answer, in seconds (the first call also loads the model).
    llm_timeout_s: float = Field(default=120, gt=0)
    # Only for the optional `anthropic` backend; never required.
    anthropic_api_key: SecretStr | None = None

    # --- Stage 4: knowledge graph (wikipulse.graph) ---
    # Neo4j (docker-compose.yml). The password must match NEO4J_PASSWORD of the container.
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr = SecretStr("wikipulse-graph")
    # Model that extracts entities and relationships from events, as `backend:model`.
    extract_model: str = "ollama:qwen3:4b"
    # Wikidata's API, for linking names that the event's own pages don't resolve.
    wikidata_api_url: str = "https://www.wikidata.org/w/api.php"
    # The extraction prompt sees at most this many pages per event (highest spike score
    # first), each trimmed to this many characters of summary and of recent changes.
    extract_max_pages: int = Field(default=6, ge=1)
    extract_max_chars: int = Field(default=1200, ge=100)
    # How often the graph runner looks for new or changed events, in seconds.
    graph_poll_interval_s: float = Field(default=30, gt=0)

    # --- Stage 4: extraction evaluation (wikipulse.eval.extraction) ---
    # Wikidata Query Service (SPARQL): which items carry a name as their label, and which
    # statements connect two items. Asked in batches, one request at a time.
    wikidata_sparql_url: str = "https://query.wikidata.org/sparql"

    # --- Stage 5: question answering over the graph (wikipulse.qa) ---
    # Model that writes Cypher and answers questions, as `backend:model`. One model for
    # both steps, so a question never swaps models on the GPU.
    qa_model: str = "ollama:qwen3:8b"
    # A generated Cypher query may return at most this many rows and run at most this
    # many seconds (the Neo4j transaction timeout).
    qa_cypher_max_rows: int = Field(default=25, ge=1, le=200)
    qa_cypher_timeout_s: float = Field(default=5, gt=0)
    # Vector search: pages and events returned per question, and the lowest cosine
    # similarity (question vs. text) that still counts as relevant.
    qa_vector_top_k: int = Field(default=5, ge=1)
    qa_vector_min_cosine: float = Field(default=0.63, ge=-1, le=1)
    # Most facts handed to the answering model (keeps the prompt inside OLLAMA_NUM_CTX).
    qa_max_facts: int = Field(default=60, ge=1)

    # --- Stage 5: question-answering evaluation (wikipulse.eval.qa) ---
    # The frozen graph snapshot the evaluation asks (the `neo4j-eval` service of
    # docker-compose.yml, filled by `scripts/snapshot_graph.py restore`). Same user and
    # password as NEO4J_USER / NEO4J_PASSWORD.
    eval_qa_neo4j_uri: str = "bolt://localhost:7688"
    # Judge models, as comma-separated `backend:model` settings. The first one gives the
    # report's scores; every further one judges the same answers, for judge agreement.
    eval_qa_judges: str = "ollama:qwen3:8b,ollama:qwen3:4b"

    # --- Stage 5: MCP server (wikipulse.mcp_server) ---
    # Address of the HTTP transport (`--transport http`). Only loopback addresses are
    # accepted: the server has no authentication.
    mcp_http_host: str = "127.0.0.1"
    mcp_http_port: int = Field(default=8765, ge=1, le=65535)
    # "Trending" = active events plus those that started this many hours before the
    # graph's newest event (event time, so a replayed day has trending events too).
    mcp_trending_hours: float = Field(default=24, gt=0)

    # --- Stage 5: analyst agent (wikipulse.agent) ---
    # Model that investigates events and writes briefings, as `backend:model`. The same
    # model as QA_MODEL by default, so the agent and the `ask` tool share one model on the
    # GPU instead of swapping two.
    agent_model: str = "ollama:qwen3:8b"
    # Let the model think before each answer (slower; see docs/learning/05d-analyst-agent.md).
    agent_think: bool = False
    # Check the finished briefing against its evidence (one more model call) and remove
    # the sentences the evidence doesn't state.
    agent_verify: bool = True
    # Controls of one run: at most this many model turns with tools, this many tokens
    # (input + output, summed over every call), this many seconds in total.
    agent_max_steps: int = Field(default=8, ge=1, le=50)
    agent_token_budget: int = Field(default=60_000, ge=1000)
    agent_run_timeout_s: float = Field(default=900, gt=0)
    # Per-tool timeouts: `ask` runs an LLM itself and needs much longer than a lookup.
    agent_tool_timeout_s: float = Field(default=30, gt=0)
    agent_ask_timeout_s: float = Field(default=180, gt=0)
    # A tool result shown to the model is cut at this many characters.
    agent_max_result_chars: int = Field(default=5000, ge=500)
    # The trigger: an event gets a briefing when its best spike score reaches
    # AGENT_MIN_SPIKE_SCORE or it has at least AGENT_MIN_PAGES pages.
    agent_min_spike_score: float = Field(default=9.0, ge=0)
    agent_min_pages: int = Field(default=3, ge=1)
    # How often the trigger looks for new significant events, in seconds.
    agent_poll_interval_s: float = Field(default=60, gt=0)

    # --- Stage 5: web API and live page (wikipulse.api) ---
    # Address of the API and the page. Only loopback addresses are accepted: there is no
    # authentication.
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    # The grouping run whose events the page shows ("live" for the live pipeline).
    api_run_id: str = "live"
    # How often the API looks for new and changed events to push over the WebSocket.
    api_poll_interval_s: float = Field(default=5, gt=0)
    # How many of the most recent events the WebSocket watches (and sends on connect).
    api_watch_limit: int = Field(default=200, ge=1, le=1000)
    # Questions to /ask run one at a time; more than this many waiting get HTTP 429.
    api_max_waiting_questions: int = Field(default=3, ge=1)

    # --- Stage 6: data lake (wikipulse.lake) ---
    # S3-compatible object storage (the `rustfs` service of docker-compose.yml). On AWS,
    # point S3_ENDPOINT at https://s3.<region>.amazonaws.com; nothing else changes.
    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "wikipulse-lake"
    s3_region: str = "us-east-1"
    s3_access_key: SecretStr = SecretStr("wikipulse")
    s3_secret_key: SecretStr = SecretStr("wikipulse-lake")
    # Key prefix (the "table") of the edit archive inside the bucket.
    lake_prefix: str = "edits"
    # Consumer group the archiver reads the edit stream with (its own position, apart
    # from the detector's).
    lake_group: str = "lake:archive"
    # How often (wall-clock seconds) buffered edits are written to the lake and
    # acknowledged, and how many buffered edits force a write earlier.
    lake_flush_interval_s: float = Field(default=300, gt=0)
    lake_max_buffer_rows: int = Field(default=50_000, ge=1)
    # Only one archiver may write to a lake at a time (a Redis lock, renewed while it
    # runs). After a hard kill the lock expires by itself after this many seconds, so a
    # restarted archiver waits at most this long. Must be well above the S3 timeouts'
    # worst case for one hour write to stay meaningful; 10 s is the minimum.
    lake_lock_ttl_s: float = Field(default=60, ge=10)

    # --- Stage 6: event-driven worker (wikipulse.workers) ---
    # The object store publishes an "object created" event on this Redis pub/sub channel
    # for every new Parquet file (set up by `uv run python -m wikipulse.workers.setup`).
    lake_notify_channel: str = "lake:notifications"
    # Redis as the *object store* reaches it: inside Docker that is the Compose service
    # name, not localhost (localhost inside the RustFS container is RustFS itself).
    lake_notify_redis_url: str = "redis://redis:6379/0"
    # Name of the notification target in the object store (its ARN ends in :<name>:redis).
    lake_notify_target: str = "wikipulse"
    # A failing handler is tried this many times in all, waiting worker_retry_delay_s,
    # then twice as long, ...; then the notification goes to the dead-letter list.
    worker_max_attempts: int = Field(default=3, ge=1)
    worker_retry_delay_s: float = Field(default=2, ge=0)
    worker_dead_letter_key: str = "workers:hourly_stats:dead"
    # How many of an hour's most edited pages `hourly_stats.top_pages` keeps.
    worker_top_pages: int = Field(default=10, ge=1)

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, value: Any) -> Any:
        """Accept `debug` as well as `DEBUG`; anything else unknown is rejected."""
        return value.strip().upper() if isinstance(value, str) else value

    def detector_config(self) -> DetectorConfig:
        """The detector thresholds from the `DETECT_*` settings."""
        prefix = "detect_"
        values = {
            name: getattr(self, prefix + name)
            for name in DetectorConfig.model_fields
            if hasattr(self, prefix + name)
        }
        return DetectorConfig(**values)

    def events_config(self) -> EventsConfig:
        """The grouping weights and thresholds from the `EVENTS_*` settings."""
        prefix = "events_"
        values = {
            name: getattr(self, prefix + name)
            for name in EventsConfig.model_fields
            if hasattr(self, prefix + name)
        }
        return EventsConfig(**values)

    def user_agent_is_placeholder(self) -> bool:
        """True if USER_AGENT still contains placeholder text instead of a real contact."""
        return any(marker in self.user_agent for marker in USER_AGENT_PLACEHOLDER_MARKERS)


@lru_cache
def get_settings() -> Settings:
    """Return the settings, loaded once and cached for the life of the process."""
    return Settings()
