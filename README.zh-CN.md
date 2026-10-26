# Aiphone WP-2MED 预录应答测试版

这是 [yufeikang/hass-aiphone](https://github.com/yufeikang/hass-aiphone) 的实验性 fork，保留原项目的配对、门铃事件、按需视频和录像功能，新增「来客按铃后，手动按一个按钮发送预录音频」。许可证和原作者署名保留。

**当前状态：本地 WebRTC 端到端音频发送经过测试，尚未在真实 WP-2MED 的门口扬声器上验证。它不是已经打通的双向通话产品，也没有 go2rtc 输出。**

## 安装与准备

1. 测试代码已包含在默认 **`master` 分支**。在 HACS 中添加自定义仓库 `https://github.com/aureole999/hass-aiphone`，类别选 Integration，安装后重启 Home Assistant。也可以下载 [master ZIP](https://github.com/aureole999/hass-aiphone/archive/refs/heads/master.zip)，将其中的 `custom_components/aiphone` 文件夹复制到 HA 配置目录的 `custom_components/` 下，然后重启。
2. 按上游说明，在室内机进入「端末追加」模式，并在 Home Assistant 的「设置 → 设备与服务 → 添加集成」中添加 Aiphone。它注册为新的客户端，仍然依赖 Aiphone 云服务。
3. 将自己的预录文件放入 HA 配置目录下的 `aiphone/replies/`。例如 `/config/aiphone/replies/reply.wav`。
4. 在该集成的「配置／选项」中设置音频文件名，默认 `reply.wav`。只填文件名，不接受 URL、目录或指向目录外的符号链接。支持 WAV、MP3、M4A、OGG、FLAC，最长 30 秒、文件最大 10 MiB。首次试验建议 3–5 秒的普通语音。

本 fork 使用原来的 `aiphone` 集成域，不能与上游版本同时安装。已有配对、实体 unique ID 和配置条目不因本次改动而重命名；更换版本前保留原版本文件以便回退。没有配置文件中的证书、私钥或音频被包含在仓库中。

## 手动应答

1. 来客按门铃，等「Doorbell state」变成 `ringing`。
2. 按「Play prerecorded reply (experimental)」按钮。没有响铃、云连接断开或已有应答正在发送时，按钮不可用；后端也会再次校验。
3. 先完整解码并校验录音，再尝试建立真实音频发布连接。协商期间只发送静音，不提前消耗录音。
4. 发布连接成功后，请求接听当前呼叫；只有观察到匹配的接听消息后，才从录音开头按实时节奏发送一次。
5. 「Reply status」显示 `preparing → connecting → accepting → sending → sent`；异常时为 `failed` 或 `stopped`。`sent` 只说明本地发送器读完音频并报告发出了 RTP 包，**不代表门口已经听到声音**。
6. 「Stop reply sender」会停止本地发送。呼叫结束、新呼叫替换旧呼叫、其它客户端接听、MQTT 断开或集成卸载也会停止发送。

没有自动接听、自动播音或主动无呼叫播音功能。只有手动按应答按钮才会尝试接听；不发送开锁命令。

## Entrance 实时视频（v0.2.0b3）

更新后重启 Home Assistant，进入 Aiphone 设备页并打开原来的 `Entrance` 摄像头实体。已有实体的名称和 unique ID 不变，无需重新配对。

- `Entrance` 现在通过 HA 原生 WebRTC 接口播放连续视频；静态预览仍使用 JPEG 抓拍。直接打开实时画面会在空闲时启动 Monitor，不会自动接听来电。
- 也可以先按 `Monitor` 再打开 `Entrance`。它们复用同一次采集；来电取流、MP4 录像与多个观看窗口通过媒体 relay 共享一条上游视频轨道。
- Monitor 默认运行至少 30 秒；仍有观看窗口时继续保持本地监看任务，最后一个窗口关闭后释放。门铃或云端仍可能主动结束会话，届时需要重新打开画面。
- 本版浏览器播放只接视频，不接麦克风或接听音频；预录语音应答按钮仍独立工作。原有录像中的音频不受浏览器播放影响。
- 修复来电结束后采集任务仍睡眠至 120 秒、导致 Monitor 被跳过的问题。

实现按 HA 2026.10.4 的原生 WebRTC API 核对，本地测试验证两个真实 WebRTC 观看端与 MP4 录像同时工作。真实 WP-2MED 页面播放仍待用户测试；外网观看还取决于网络的 ICE/TURN 连通性。本实现不提供 RTSP URL 或 HLS 播放地址，也不会把浏览器流声明成支持 `camera.record` 的 HLS 流。

## 待实机验证的协议环节

- 发布流程使用来电 `CID`，基于上游主动监看中的 `30001/31001` 发布流程改造；此顺序是否被 WP-2MED 的真实来电会话接受，尚未验证。
- 接听通过 `24000 RSLT=200` 请求，并等待匹配的 MQTT 事件。该消息可能只是云端转发，并不证明音频已到门口；如果固件不回传该事件，发送器会超时，不播放录音。
- 编码由实际 WebRTC SDP 协商决定，没有强制沿用其它摄像头的 PCMA、采样率或分包时长。
- 本实现会释放本地 WebRTC 连接与媒体资源，**没有臆造云端挂断命令**。播完或按停止后，共享呼叫可能仍持续到设备超时，或需要在官方 App／室内机结束。
- 上游报告官方 App 与第三方可能争用同一次呼叫的视频订阅。这个 fork 仍保留上游被动视频捕获；与手机 App 的共存、语音实际去向、音量和断线行为均需实测。

## 测试

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pytest -q
```

测试使用临时生成的音视频和本机真实 aiortc 端点，验证音频首尾样本、实时节奏、静音门控、RTP 解码、双观看端与录像共享视频，以及配对和采集资源释放。呼叫信令由测试内的替身提供，覆盖缺失文件、无来电、重复点击和取消等场景；不会连接 Aiphone 云端或门铃。Home Assistant 的完整安装／配对及物理扬声器试听不在本地测试覆盖范围内。
