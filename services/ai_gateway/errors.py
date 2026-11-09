"""Safe execution errors shared by in-process and optional HTTP adapters."""


class ModelExecutionError(Exception):
    def __init__(self, status_code: int, *, detail: dict):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail.get('code', 'model_execution_failed'))
