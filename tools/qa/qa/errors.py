class QAError(Exception):
    exit_code = 3


class BehaviorFailure(QAError):
    exit_code = 1


class PreconditionFailure(QAError):
    exit_code = 2


class UsageError(QAError):
    exit_code = 64
