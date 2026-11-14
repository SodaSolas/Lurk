# 黑听（Lurk）

B 站直播后台听：不开浏览器、不看画面，挂在 Windows 11 任务栏上「黑听」。

参考 [FluentFlyout](https://github.com/unchihugo/FluentFlyout) 的任务栏媒体组件，把 B 站直播的声音放进任务栏。只想听直播的时候，可以完全关掉浏览器。

## 功能

- 任务栏小组件显示主播名和播放状态，右侧按钮直接播放或停止。全屏应用和任务栏自动隐藏时，它跟任务栏一起隐藏。
- 点击小组件展开 Fluent 风格的亚克力面板：
  - 输入房间号、直播链接或 b23.tv 短链收听。
  - 调音量、收藏房间、查看最近听过的 50 个房间，或用默认浏览器打开当前直播间。
  - 用哔哩哔哩 App 扫码登录后，查看关注中正在开播的主播，点一下直接收听。
- 跟随系统浅色、深色和强调色，切换后即时生效。
- 右下角的设置：

  | 设置 | 默认 | 说明 |
  | --- | --- | --- |
  | 开机自动启动 | 关 | 写入当前用户的 `Run` 注册表项 |
  | 断线自动重连 | 开 | 间隔 2、4、8、15、30 秒最多重试 5 次；发现已下播就停止 |
  | 媒体键控制 | 开 | 接入 Windows 系统媒体控制：键盘媒体键，音量浮窗显示主播、标题和头像。直播无法暂停，暂停等同停止 |
  | 滚轮调音量 | 开 | 在任务栏小组件上滚动，每格 5 |
  | 显示主播头像 | 开 | 正在播放卡片显示 B 站头像，本机缓存一周 |
  | 跟随系统强调色 | 开 | 关闭时使用默认蓝色 |

## 安装

需要 Windows 11 和 Python 3.12。

1. 克隆本仓库，在目录里创建虚拟环境并安装依赖：

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   PySide6 里有些文件路径很深。如果 pip 报「No such file or directory」并提示 Long Path，把项目放到短一点的路径下（比如 `C:\Dev\Lurk`），或开启 Windows 长路径支持。

2. 下载 Windows 版 [mpv](https://mpv.io/installation/)，解压到本目录的 `mpv` 文件夹，确保存在 `mpv\mpv.exe`。
3. 编译启动器 `Lurk.exe`（用系统自带的 .NET Framework 编译器）：

   ```powershell
   & "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:winexe /codepage:65001 /reference:System.Windows.Forms.dll /out:Lurk.exe Launcher.cs
   ```

## 使用

双击 `Lurk.exe`，也可以给它创建快捷方式。EXE 要留在项目目录里，它靠相对路径找到 `.venv` 和程序文件。不想编译的话，可以用 `Start Lurk.vbs` 启动。

- 收起面板后，点任务栏上的小组件重新打开；再次运行启动器也会唤出面板。
- 点面板右上角的电源按钮退出，同时关闭播放器。
- 设置、收藏、历史和头像缓存保存在 `%LOCALAPPDATA%\BiliLiveAudio`。登录凭据用 Windows DPAPI 加密，只有当前 Windows 用户能解开；「退出登录」会删除它。

命令行原型 `listen.py` 可以单独使用，默认播放房间 `21292831`，按 `Ctrl+C` 停止：

```powershell
.\.venv\Scripts\python.exe .\listen.py 房间号或链接
```

## 工作原理

- **播放**：Streamlink 拉取直播流，通过管道交给 mpv（`--no-video`）只解码音频。两个进程放在 kill-on-close 的 Job Object 里，程序退出或崩溃时会一起结束。音量通过 mpv 的 IPC 命名管道实时调整。
- **任务栏嵌入**：小组件和 FluentFlyout 一样，是任务栏 `Shell_TrayWnd` 的子窗口，并且必须是分层窗口（layered），否则会被 Windows 11 的 XAML 任务栏盖住。程序定时把它摆回任务栏顶层，任务栏窗口变了也会尝试重新挂上。
- **亚克力面板**：用 DWM 的 `DWMWA_SYSTEMBACKDROP_TYPE` 开启系统背景。Qt 会把「无边框 + 透明背景」的窗口做成分层窗口，而 DWM 不给分层窗口画系统背景，所以面板保留标题栏样式，在 `WM_NCCALCSIZE` 里去掉边框。
- **系统媒体控制**：用一个关闭了命令管理器的 WinRT `MediaPlayer` 取得自己的媒体会话，声音仍由 mpv 播放。缺少 winrt 包时其他功能照常，只是媒体键不可用。

## 流量

B 站没有单独的音频流，游客能拿到的最低画质是 qn=250，各协议和 CDN 都一样。所以虽然只播放声音，下载的仍是带视频的流。实测一个房间音频约 164 kbps、视频约 366 kbps；HEVC 流只省约 10%。

## 限制

- 小组件只放在主屏的横向任务栏上；空位不够时退回为独立面板。竖向任务栏和 Explorer 重启后的重新挂载还没有实测。
- 只支持 Windows 11。

## 声明

本项目与哔哩哔哩无关，仅供个人学习和使用。直播内容的版权归主播和平台所有。

## 许可

[MIT](LICENSE)
