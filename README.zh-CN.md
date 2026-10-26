# Aiphone WP-2MED：Monitor、来电双向通话与预录应答

这是 [yufeikang/hass-aiphone](https://github.com/yufeikang/hass-aiphone) 的 fork，由 [aureole999](https://github.com/aureole999/hass-aiphone) 维护。保留原集成的配对、实体身份、门铃事件、快照和录像，按官方 Type A App 的信令流程实现通话。

v0.2.0b7 已通过本机真实 WebRTC 音视频与协议回归测试；WP-2MED 实际扬声器、回声、音量和云端兼容性需要用户实测。原生 Entrance 摄像头继续工作，无需重新配对。本项目尚不提供 RTSP、go2rtc 或 HomeKit 双向通话输出。

## 更新和安装

在 HACS 添加自定义仓库 `https://github.com/aureole999/hass-aiphone`，类别 Integration，选择最新测试版本并重启 Home Assistant。本 fork 与原项目使用同一个 `aiphone` 域，不能同时安装。回退时在 HACS 重新下载先前版本并重启，配对数据和已有实体 unique ID 保留。

首次使用需要在室内机进入「端末追加」后，在 HA「设置 → 设备与服务」添加 Aiphone。该集成使用 Aiphone 云服务；首次配对需要 HA 能通过局域网发现室内机。

## 通话卡片

当前 HA 版本的普通摄像头播放器可能没有麦克风按钮。集成附带无外部依赖的 `aiphone-intercom-card`，使用 HA 原生、已认证的摄像头 WebRTC 接口。

1. HA「设置 → 仪表盘 → 资源」添加资源（需启用高级模式）：
   - URL：`/aiphone-static/aiphone-intercom-card.js?v=0.2.0b7`
   - 类型：JavaScript Module。
2. 在仪表盘添加手动卡片，填入下方配置。`entity` 使用自己的 Entrance 摄像头实体。

```yaml
type: custom:aiphone-intercom-card
entity: camera.aiphone_wp_2med_entrance
title: 门口对讲
```

卡片默认通过实体注册表寻找同一集成的 Hang up 按钮。如果当前用户不能读取实体注册表，可以显式配置：

```yaml
hang_up_entity: button.aiphone_wp_2med_hang_up
```

操作方式：

- **Monitor**：只打开画面，不申请麦克风、不自动接听。WP-2MED 在这个状态可能不发送门口声音，与官方 App 一样。
- **通话 / 接听**：用户同意浏览器麦克风权限后，Monitor 转为通话；如果正在响铃则接听当前来电。支持边看视频、边听门口声音、边向门口讲话。
- **麦克风静音 / 恢复麦克风**：仅控制本端麦克风，门口声音继续播放。
- **挂断 / 关闭**：停止麦克风、关闭浏览器连接，退出媒体会话并发送断开指令；未接听的来电按官方流程发送拒接应答。
- 关闭正在通话的卡片或页面也会释放麦克风并结束该通话。关闭一个普通观看窗口不会挂断另一个窗口的通话。

麦克风需要通过 **HTTPS** 打开 HA，并允许麦克风权限。播放器若因浏览器自动播放策略无声，可点击视频播放或音量控件。外网媒体连通还取决于浏览器与 HA 的 ICE 网络路径；当前浏览器桥接没有配置 TURN 中继。

## 原有实体

- `Entrance`：原生 HA WebRTC 音视频、JPEG 快照。
- `Monitor`：启动监看；普通观看至少保持 30 秒，最后一个观看窗口关闭后留 20 秒供播放器切换，再退出云端会话。
- `Answer`：来电接听，或 Monitor 转通话；此实体本身不能取得手机/电脑麦克风。需要说话时使用通话卡片。
- `Hang up`：结束当前 Monitor 或通话。
- 门铃状态：`idle`、`ringing`、`answered`。收到呼叫通知不会被误判成已接听。
- 原有开锁实体保持原实现。本次通话测试不调用开锁。

多个观看端与 MP4 录像通过媒体 relay 共享同一次上游接收。仅允许一个麦克风或一个预录发送器占用上行；第二个客户端不能覆盖当前麦克风。其它官方 App 接听时，本集成释放自己的媒体连接，不发送会挂断对方的断开请求。

## 来电后发送预录语音

1. 将自己的音频放入 `/config/aiphone/replies/`，例如 `reply.wav`。
2. 在集成选项填写文件名。支持 WAV、MP3、M4A、OGG、FLAC，最长 30 秒、最大 10 MiB；不接受 URL、目录或指向目录外的符号链接。
3. 来客按铃后，按原有「Play prerecorded reply」按钮。后端先完整解码文件，再按正确的来电流程接听并发送一次。
4. 播完或按「Stop reply sender」后释放媒体并挂断。没有自动播音或无呼叫时主动播音。

`Reply status` 的 `sent` 表示本地发送完成，不等于物理扬声器已被听见。预录与实时麦克风使用同一个 PCMU 发布器，不再建立独立、竞争的错误来电会话。

## 协议与音频

| 场景 | 流程 |
| --- | --- |
| 来电收到通知 | `23001 → 24000`；不代表接听 |
| 来电画面准备 | `30011/31011 → 30012/31012 → 24001` |
| 来电接听 | `30021/31021` 建立发送方，随后 `24002 RSLT=200 → 25001` |
| 主动 Monitor | `30001/31001 → 23021/24021 → 30011/31011 → 30012/31012` |
| Monitor 按电话 | `23022 → 24022`，再放行上行语音 |
| 主动挂断 | `30031/31031 → 23003/24003` |

上行强制 **PCMU / G.711 μ-law，8 kHz、单声道、20 ms**：每包 160 字节，RTP PT 0、时间戳每包增加 160。浏览器音频可以协商 Opus，由 HA 端解码、重采样后编码为 PCMU 发给门铃，SRTP 加密由 aiortc 完成。协商未成功前只提供静音。实时麦克风队列最多保留两帧，避免旧音频积压。

集成诊断提供各 PeerConnection 状态、RTP 收发计数、音频帧计数及最多 128 条脱敏信令。不含证书、私钥、SDP、音频内容、MQTT 主题或原始会话标识。

## 本地开发测试

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pytest -q
node --test tests/frontend.test.cjs
```

Python 测试通过本机真实 aiortc 端点验证两种通话的双向音频、PCMU 分包/时间戳、共享录像、拒绝、错误会话、其它客户端接听、重复事件与资源释放。卡片测试覆盖麦克风权限拒绝、显式申请、静音、挂断、错误提示和离开页面。所有自动测试均不连接门铃或 Aiphone 云端。
