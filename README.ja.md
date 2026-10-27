# Aiphone WP-2MED for Home Assistant

ライブ映像、キャッシュ画像、呼出センサー、双方向通話、クイック返信に対応します。

HACS でインストール後、Home Assistant を再起動し、**設定 → デバイスとサービス**から **Aiphone WP-2MED** を追加します。室内機を「端末追加」にしてペアリングしてください。同じ LAN と UDP 51711/51712 へのアクセスが必要です。通常の通信には Aiphone クラウドを使用します。Scrypted と併用する場合は、それぞれ別の端末として登録してください。

ダッシュボードのリソースに `/aiphone-static/aiphone-intercom-card.js?v=0.2.0b8` を JavaScript Module として追加します。

```yaml
type: custom:aiphone-intercom-card
entity: camera.aiphone_wp_2med_entrance
```

**Monitor** で映像を表示し、**Talk / Answer** でマイクを有効にします。マイクには HTTPS とブラウザーの許可が必要です。

統合オプションで解錠、クイック返信、呼出センサー、録画を個別に設定できます。返信は待機・モニター・通話中に使用でき、音声接続を待って再生します。再生後も通話は継続します。音声ファイル名を空欄にすると内蔵の日本語メッセージを使用します。独自の音声は `/config/aiphone/replies/` に配置してください。

実験的な設定として、ライブ映像を開いたときの自動応答（オフ・呼出中のみ・常時）と、接続状態を重ねて表示するステータス映像を利用できます。マイクは手動で有効にしてください。

静止画像の取得は映像接続を開始しません。更新時は既存のペアリングとエンティティ ID を維持します。詳細は [English README](README.md) を参照してください。
