from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    # Feishu / lark-cli
    LARK_CLI_PATH: str = "lark-cli"
    LARK_CLI_TIMEOUT: int = 30
    LARK_CLI_AS: str = "bot"

    # Calendar polling
    PREMEET_PUSH_MINUTES: int = 25
    PREMEET_LOOKAHEAD_HOURS: int = 2
    PREMEET_POLL_INTERVAL: int = 60
    CALENDAR_ID: str = "primary"

    # LLM
    LLM_PROVIDER: str = "anthropic"  # anthropic | openai | doubao
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = ""
    DOUBAO_API_KEY: str = ""
    DOUBAO_BASE_URL: str = "https://ark.cn-beijing.volces.com/api/v3"
    LLM_MODEL: str = "claude-sonnet-4-6"
    LLM_MAX_TOKENS: int = 4096
    LLM_TEMPERATURE: float = 0.2

    # Wiki
    WIKI_SPACE_ID: str = ""
    WIKI_MAX_DOCS: int = 5
    WIKI_SEARCH_TIMEOUT: int = 15
    WIKI_QUALITY_THRESHOLD: float = 0.3
    WIKI_CONFIDENCE_THRESHOLD: float = 5.0

    # Vector / hybrid search
    VECTOR_SEARCH_ENABLED: bool = True
    DOUBAO_EMBEDDING_MODEL: str = ""       # Doubao embedding endpoint ID, e.g. ep-xxxx-embed
    DOUBAO_EMBEDDING_API_KEY: str = ""     # separate API key for embedding endpoint (if different from DOUBAO_API_KEY)
    EMBEDDING_DIMS: int = 2048             # doubao-embedding-large default dims
    EMBEDDING_CACHE_TTL_DAYS: int = 7
    VECTOR_TOP_K: int = 5                 # max extra docs vector path adds to candidate pool
    # Offline index
    INDEX_MAX_DOCS: int = 200             # max docs to crawl per rebuild
    INDEX_RECENT_DAYS: int = 90           # only index docs updated within N days

    # Event subscription
    EVENT_FILTER: str = r"vc\.meeting"
    IM_MESSAGE_FILTER: str = r"im\.message"

    # Q&A Agent
    QA_CONTEXT_TTL_SECONDS: int = 4 * 3600

    # IM context for pre-meeting
    IM_CONTEXT_DAYS: int = 14
    IM_CONTEXT_MAX_MESSAGES: int = 10
    MY_OPEN_ID: str = ""  # self open_id to exclude from DM targets

    # Task defaults
    TASK_DEFAULT_DUE_DAYS: int = 3

    # Set to False to disable post-meeting task creation (safe mode for guest/demo users)
    TASK_CREATION_ENABLED: bool = True


settings = Settings()
