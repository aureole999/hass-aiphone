# Aiphone WP-2MED for Home Assistant

支持实时视频、缓存快照、响铃传感器、双向通话和快捷回复。

通过 HACS 安装后，重启 Home Assistant，在「设置 → 设备与服务」中添加 **Aiphone WP-2MED**。将室内机切换至「端末追加」模式，按提示配对。配对需要同一局域网及 UDP 51711/51712；日常通信使用 Aiphone 云端。与 Scrypted 同时使用时，请分别配对独立终端。

在仪表盘资源中以 JavaScript Module 添加 `/aiphone-static/aiphone-intercom-card.js?v=0.2.0b8`：

```yaml
type: custom:aiphone-intercom-card
entity: camera.aiphone_wp_2med_entrance
```

**Monitor** 打开监看，**Talk / Answer** 开启麦克风。使用麦克风需要 HTTPS 和浏览器授权。

集成选项提供：

- 分别启用开锁、快捷回复和响铃传感器。
- 快捷回复支持待机、监看和通话状态，等待音频就绪后播放；临时接管麦克风，结束后保留通话。文件名留空使用内置日语回复；自定义音频放入 `/config/aiphone/replies/`。
- 自动接听（实验）：关闭、仅响铃时、始终。打开实时画面时生效，麦克风仍需手动开启。
- 状态画面（实验）：连接期间显示缓存图片，连接后显示实时视频及通话状态。
- 可选保存录像。

快照不会主动开启视频连接。升级保留已有配对和实体 ID。详情见 [English README](README.md)。
