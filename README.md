# Magpie Android Companion

这是 [yetone/magpie](https://github.com/yetone/magpie) 的独立 Android 伴侣项目。Mac 上运行上游 Magpie，Android 通过 USB 打开同一台 Mac 提供的 Web 管理界面。上游 Magpie 由 yetone 和贡献者维护，采用 [MIT 许可证](https://github.com/yetone/magpie/blob/main/LICENSE)。本仓库包含 Android 客户端和 Mac 桥接代码。

## 功能

- USB 调试授权后自动配对。手机可以查看用量、请求、会话和智能体，并使用上游 Web 界面提供的管理操作。
- 横屏使用左侧导航，竖屏使用底部导航。手机设置可独立控制状态栏和系统导航栏。
- 局域网支持配对后查看原生摘要。完整 Web 管理通过 USB 提供。蓝牙与外部 OAuth 登录尚未接入。

## 构建和连接

使用 Java 21、Android SDK Platform 36、Build Tools 36.0.0 和项目自带的 Gradle Wrapper。Mac 上的 Android SDK 路径可通过 `ANDROID_HOME` 或本机的 `local.properties` 配置。

```bash
./gradlew :app:assembleDebug
```

正式版需要本机的 `signing/magpie-galph-release.p12`。构建安装脚本通过 `MAGPIE_SIGNING_PASSWORD` 或交互式提示读取密码。USB 自动配对会用仓库中的公开证书校验已安装 APK。

1. 启动 Mac 版 Magpie，在设置中将 Browser access 设为本机访问。
2. 将已授权 USB 调试的 Android 手机连接到 Mac。
3. 运行下列命令安装正式版并启动完整 USB 管理界面。

```bash
bash tools/setup_mac_android.sh
bash tools/usb_start.sh --full-browser
```

摘要桥和局域网模式的参数见 [Mac 桥接说明](host/README.txt)。

## 检查

```bash
python3 -B -m unittest discover -s host -q
python3 -B -m unittest discover -s tools -q
```

真机安装正式版并启动 USB 桥接后，可用 `python3 -B tools/device_browser_check.py` 检查完整界面的冷启动和用量页。
