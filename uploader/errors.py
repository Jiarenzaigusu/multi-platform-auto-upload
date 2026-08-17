class PublishResultUncertainError(RuntimeError):
    """发布结果不确定异常。

    当发布按钮已被点击，但 30 秒内无法从平台页面确认成功或失败时抛出。
    此异常会触发任务进入 "uncertain"（结果待核对）状态，提醒运营人员
    必须先到平台后台人工核对，确认前不要重试，避免重复发布。
    """
