class YokazeError(Exception):
    """Base error for structured UI/CLI handling."""

class DeviceBusyError(YokazeError):
    pass

class FeederEmptyError(YokazeError):
    pass

class CropFailedError(YokazeError):
    pass

class DeliverFailedError(YokazeError):
    pass
