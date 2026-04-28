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

    # Event subscription
    EVENT_FILTER: str = r"vc\.meeting"

    # IM context for pre-meeting
    IM_CONTEXT_DAYS: int = 14
    IM_CONTEXT_MAX_MESSAGES: int = 10

    # Task defaults
    TASK_DEFAULT_DUE_DAYS: int = 3


settings = Settings()
