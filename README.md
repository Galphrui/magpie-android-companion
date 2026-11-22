# Magpie Android Companion

最近发现Magpie真的是一个神仙项目，越用越上头，想着做一个外接扩展屏专门放这玩意。后边转念一想，我本身就是做Android的，刚好家里还有不用的旧手机，直接把功能移植手机版多好。说干就干，借助AI简直事半功倍。

这是 [yetone/magpie](https://github.com/yetone/magpie) 的独立 Android 伴侣项目。Windows 或 Mac 运行上游 Magpie，Android 使用蓝牙连接到同一台电脑的 Web 管理界面。上游 Magpie 由 yetone 和贡献者维护，采用 [MIT 许可证](https://github.com/yetone/magpie/blob/main/LICENSE)。本仓库包含 Android 客户端和桌面桥接代码。

## 功能

- 手机扫描桌面程序显示的二维码，通过低功耗蓝牙配对。手机可以查看用量、请求、会话和智能体，并使用上游 Web 界面提供的管理操作。
- macOS 仍可使用 USB。USB 调试授权后自动配对。
- 横屏使用左侧导航，竖屏使用底部导航。手机设置可独立控制状态栏和系统导航栏。
- 局域网支持配对后查看原生摘要。外部 OAuth 登录在内嵌 Web 界面中尚未接入。

## 蓝牙连接

桌面需要 Windows 10 1709 或更新版本、Windows 11，或 macOS，以及支持低功耗蓝牙的适配器。使用带 Tk 的 Python 3.10+。Windows 的 Python 安装选项中需包含 Tcl/Tk。macOS 启动脚本需要 Framework 版 Python，可使用 [python.org 安装包](https://www.python.org/downloads/macos/)。

1. 启动上游 Magpie，在「设置 → 网络与共享 → 浏览器访问」选择「本机」。
2. Windows 双击 [start-bluetooth.cmd](tools/start-bluetooth.cmd)。macOS 双击 [start-bluetooth.command](tools/start-bluetooth.command)。首次启动在用户目录创建 Python 环境并下载安装依赖。macOS 首次连接时，请在系统提示中允许 Magpie 蓝牙配置使用蓝牙。
3. 打开 Android 伴侣端，在手机设置中选择「扫描蓝牙二维码」。授权后扫描桌面程序显示的二维码，也可以在扫描页选择「从图片识别」。
4. 配对后，手机会先同步智能体、网关和路由摘要。要查看用量、请求或会话，请点「打开完整电脑界面」。保持桌面程序运行。停止连接再启动会生成新二维码，需要重新扫描。

安装版 Magpie 的浏览器配置通常位于用户目录下的 `.config/magpie/web-page.json`。使用自定义配置时，启动桌面程序前将 `MAGPIE_CONFIG_DIR` 设为包含 `web-page.json` 的目录。蓝牙连接使用二维码中的配对凭据，Web 管理流量通过 TLS 传输。配对期间请把二维码留在自己的电脑屏幕上。

APK 内置 Magpie v0.1.4622 的版本化 JavaScript 和 CSS。完整电脑界面直接读取哈希匹配的资源，并通过蓝牙获取当前数据。桌面 Magpie 更新后，改变的资源由配对电脑提供。更新内置资源时，从对应版本的上游 Git checkout 运行 `python3 tools/build_web_asset_cache.py /path/to/magpie`。

## 构建与 USB 连接

使用 Java 21、Android SDK Platform 36、Build Tools 36.0.0 和项目自带的 Gradle Wrapper。Mac 上的 Android SDK 路径可通过 `ANDROID_HOME` 或本机的 `local.properties` 配置。

```bash
./gradlew :app:assembleDebug
```

正式版需要本机的 `signing/magpie-galph-release.p12`。构建安装脚本通过 `MAGPIE_SIGNING_PASSWORD` 或交互式提示读取密码。USB 自动配对会用仓库中的公开证书校验已安装 APK。

1. 启动 Mac 版 Magpie，在设置中将 Browser access 设为本机访问。
2. 将已授权 USB 调试的 Android 手机连接到 Mac。
3. 在 Mac 终端运行安装命令。安装完成后会注册当前用户的 LaunchAgent，每分钟检查 ADB；检测到已授权实体手机时自动建立 USB 反向通道、导入短时配对信息并启动/切换 Android APP，无需在扫描页手动操作。

```bash
bash tools/setup_mac_android.sh
```

如需排查，可在不运行自动服务时手动启动桥接：`bash tools/usb_start.sh --full-browser`。请勿同时运行手动桥接与 LaunchAgent。

摘要桥和局域网模式的参数见 [Mac 桥接说明](host/README.txt)。

## 检查

```bash
python3 -B -m unittest discover -s host -q
python3 -B -m unittest discover -s tools -q
```

真机安装正式版并启动 USB 桥接后，可用 `python3 -B tools/device_browser_check.py` 检查完整界面的冷启动和用量页。

## 后续

接下来首先会在windows上试运行，然后补充功能。可能会添加远程的功能，彻底实现设备端控制终端。不过可能涉及搭建服务器什么的，再议吧。各位大佬有什么好的想法或者补充欢迎完善。
