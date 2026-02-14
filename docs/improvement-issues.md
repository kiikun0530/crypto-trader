# 改善課題一覧

システム全体レビュー (2026-02-11) の結果をまとめたもの。
重要度: 🔴 Critical / 🟡 Medium / 🟢 Low

ステータス: ✅ 完了 / ⏭️ 無視（対応不要） / 📋 未対応

---

## 🔴 Critical

### ISSUE-001: order-executor の Timeout ドリフト ✅ 完了

- **現状**: Terraform定義では `timeout = 60` だが AWS実値が 30秒にドリフトしていた
- **対処**: `terraform apply -target` で 60秒に修正済み (2026-02-11)

### ISSUE-002: price-collector / position-monitor で 2/6-7 に大量エラー ⏭️ 無視

- **現状**: 2/6に1,130件、2/7に579件のエラー。2/8以降は0件で収束済み
- **判断**: 一時的な障害で自然収束。対応不要

### ISSUE-003: slack-notifier Lambda の管理状態 ⏭️ 問題なし

- **現状**: 当初「Terraform管理外」と報告したが、**調査の結果 `terraform/sns.tf` で正しく管理されていた**
- **詳細**: `count = var.slack_webhook_url != "" ? 1 : 0` で条件付き作成。SNS (alerts + notifications) にサブスクライブ済み。直近7日で16回呼び出し実績あり
- **判断**: 正常稼働中。対応不要

---

## 🟡 Medium

### ISSUE-004: Terraform State がローカル管理 ✅ 完了

- **現状**: S3 backend + state locking に移行済み (2026-02-11)
- **対処**:
  - S3 バケット `eth-trading-terraform-state-652679684315` 作成（バージョニング有効）
  - DynamoDB `terraform-locks` テーブル作成
  - `main.tf` の backend ブロックを有効化（`use_lockfile = true`）
  - `terraform init -migrate-state` でローカル → S3 に移行完了

### ISSUE-005: positions テーブルに TTL が設定されていない ✅ 完了

- **現状**: positions テーブルに TTL を追加済み (2026-02-11)
- **対処**:
  - `dynamodb.tf` に `ttl { attribute_name = "ttl", enabled = true }` 追加
  - `order-executor/close_position()` でクローズ時に `ttl = timestamp + 180日` を設定
  - アクティブポジション（`closed=false`）はTTLフィールドなし → 永続保存
  - `terraform apply` で適用済み

### ISSUE-006: DynamoDB クエリの非効率なフィルタリング 📋 未対応

- **現状**: `Limit=10` + Python側フィルタリングでアクティブポジション検索
- **影響**: ISSUE-005 の TTL 追加により、クローズ済みポジションは180日後に自動削除されるため、データ膨張リスクは大幅に軽減。現時点では対応不要
- **将来**: 運用データが増加した場合は GSI 追加を検討

### ISSUE-007: 全 Lambda で共通の IAM ロール ⏭️ 無視

- **判断**: 個人運用のため最小権限分離は過剰。対応不要

### ISSUE-008: Slack Webhook URL が環境変数に平文で格納 📋 未対応

- **影響**: Slack Webhook は書き込み専用のため情報漏洩リスクは低い。優先度低

### ISSUE-009: テストコードが存在しない ⏭️ 無視

- **判断**: 現時点では対応しない

### ISSUE-010: analysis_state テーブルの用途混在 📋 未対応

- **影響**: 現状の使用量では問題なし。将来的にテーブル分離を検討

---

## 🟢 Low

### ISSUE-011: Coincheck API のエラーハンドリング不足 📋 未対応

- **備考**: `trading_common.py` の共有モジュール化により、今後のリトライ追加が容易に

### ISSUE-012: Aggregator の Slack通知内で Coincheck API を呼び出し 📋 未対応

### ISSUE-013: コード重複 — TRADING_PAIRS 定義が全サービスに散在 ✅ 完了

- **対処**: `lambda_layer/python/trading_common.py` に共通モジュールを作成 (2026-02-11)
- **統合した項目**:
  - `TRADING_PAIRS` 設定パース（6サービスから統合）
  - `get_current_price()` Coincheck API呼び出し（3サービスから統合）
  - `get_active_position()` ポジション取得（3サービスから統合）
  - `get_currency_from_pair()` ユーティリティ
  - `send_slack_notification()` Slack通知送信
  - テーブル名定数 (`PRICES_TABLE`, `POSITIONS_TABLE`, `TRADES_TABLE` 等)
- **更新サービス**: price-collector, position-monitor, order-executor, aggregator, news-collector, warm-up
- **デプロイ**: Lambda Layer + 全Lambda関数を `terraform apply` で更新済み

### ISSUE-014: `get_current_price()` 関数の重複定義 ✅ 完了

- ISSUE-013 に統合して対応済み

### ISSUE-015: Python ランタイムのバージョン 📋 未対応

- Python 3.11、EOL 2027年10月。AWS Lambda サポートはEOL後も継続見込み

### ISSUE-016: Step Functions のエラーハンドリング不足 ✅ 完了

- **対処**: 全Task ステートに Retry/Catch ブロックを追加 (2026-02-11)
- **Retry**: `Lambda.ServiceException`, `Lambda.AWSLambdaException`, `Lambda.SdkClientException`, `Lambda.TooManyRequestsException` に対して 3回リトライ（2秒間隔、バックオフ2倍）
- **Catch**: Parallel ステートと Map ステートに `States.ALL` の Catch を追加。Aggregator にも Catch 追加
- **新規ステート**: `AnalysisFailed` (失敗時の終了ステート) を追加
- **デプロイ**: `terraform apply` で適用済み

### ISSUE-017: daily-reporter の改善テーブル参照が未使用 📋 未対応

### ISSUE-018: `urllib.request` の直接使用 📋 未対応

- **備考**: Lambda Layer に `requests` は含まれているが、各サービスは `urllib.request` を使用中

### ISSUE-019: CI/CD パイプラインが未構築 ⏭️ 無視

- **判断**: 現時点では対応しない

### ISSUE-020: ドキュメントのコスト記載が古い ✅ 完了

- **対処**: README.md, architecture.md のコスト記載を $11/月 に更新 (2026-06-15)

---

## 対応サマリー (2026-02-11)

| Issue | 内容 | ステータス |
|-------|------|-----------|
| ISSUE-001 | order-executor timeout | ✅ 完了 |
| ISSUE-002 | price-collector エラー | ⏭️ 無視 |
| ISSUE-003 | slack-notifier | ⏭️ 問題なし |
| ISSUE-004 | tfstate S3移行 | ✅ 完了 |
| ISSUE-005 | positions TTL | ✅ 完了 |
| ISSUE-006 | DDBクエリ効率 | 📋 ISSUE-005で軽減 |
| ISSUE-007 | IAM分離 | ⏭️ 無視 |
| ISSUE-008 | Slack Webhook平文 | 📋 低優先 |
| ISSUE-009 | テストなし | ⏭️ 無視 |
| ISSUE-010 | テーブル用途混在 | 📋 低優先 |
| ISSUE-011 | Coincheck リトライ | 📋 低優先 |
| ISSUE-012 | Aggregator内API呼出 | 📋 低優先 |
| ISSUE-013 | コード重複 | ✅ 完了 |
| ISSUE-014 | get_current_price重複 | ✅ 完了 |
| ISSUE-015 | Python 3.11 | 📋 低優先 |
| ISSUE-016 | SF Retry/Catch | ✅ 完了 |
| ISSUE-017 | 改善テーブル未使用 | 📋 低優先 |
| ISSUE-018 | urllib.request直接使用 | 📋 低優先 |
| ISSUE-019 | CI/CD未構築 | ⏭️ 無視 |
| ISSUE-020 | コスト記載古い | ✅ 完了 |

**完了: 7件** / 無視: 5件 / 未対応: 8件
