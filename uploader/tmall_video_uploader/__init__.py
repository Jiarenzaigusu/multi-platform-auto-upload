# uploader.tmall_video_uploader 包初始化
#
# 该子模块实现淘宝光合（creator.guanghe.taobao.com）平台的视频发布自动化。
# 包含两个文件：
#   - session.py: 天猫专属浏览器会话池（TmallBrowserSession / TmallSessionPool）
#   - main.py:   天猫视频发布器主逻辑（TmallVideo 及 Cookie 校验、登录、发布流程）
