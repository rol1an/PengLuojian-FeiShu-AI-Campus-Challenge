class FeishuAutomationError(Exception):
    pass


class LarkCLIError(FeishuAutomationError):
    def __init__(self, message: str, cmd: list[str] | None = None, stderr: str = ""):
        super().__init__(message)
        self.cmd = cmd
        self.stderr = stderr


class LLMError(FeishuAutomationError):
    pass


class WikiSearchError(FeishuAutomationError):
    pass
