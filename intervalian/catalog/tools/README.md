# Intervalian カタログ lint

`intervalian/catalog/v1/`(index.json + entries/*.json)をアプリが読めるかどうかを、公開前に機械的に確認するツール。
アプリは index の1件が不正だと index 全体を読み込めない。公開前に必ず実行すること(公開の流れは下の「公開の流れ」を参照)。

標準ライブラリのみの Python 3(3.9 以上)。外部依存・ネットワークアクセスなし。

## 実行方法

リポジトリのルートで:

```sh
python3 -B intervalian/catalog/tools/lint_catalog.py            # 既定は intervalian/catalog/v1
python3 -B intervalian/catalog/tools/lint_catalog.py --root path/to/v1
python3 -B -m unittest discover -s intervalian/catalog/tools -p 'test_*.py' -v
```

終了コード 0 = エラーなし(WARN は出ることがある)、1 = エラーあり。出力は `ERROR <ファイル>: [ルール名] 内容` / `WARN ...` と末尾の集計1行。
同じ検査が GitHub Actions(`.github/workflows/pages.yml`)でも走る。push の前にローカルで先に実行しておくと、lint 失敗でデプロイが止まる事態を避けられる。

## 検査内容

アプリのコードが正(ルール名は出力の `[ ]` 内)。

ERROR(ビルド失敗):

- index.json: UTF-8 の正しい JSON・重複キーなし。`catalogVersion` = 1、`updatedAt` が日時として解釈可能(`index-field`, `catalog-version`, `json`, `utf8`)
- index の各エントリ: アプリの `CatalogEntry.fromJson` と同じ必須項目と型。title / description は `en` 必須、`language` が `ja` なら `ja` も必須(`index-field`, `locale-text`)
- id の重複なし(`duplicate-id`)
- `entryPath`: 相対パス、`..`・スキーム・`//`・`%` 等を含まない、`entries/` 配下の実在ファイル(大文字小文字も一致)(`entry-path`)
- routine: `menuEntryIds` が実在する menu の id だけを指し、`menuCount` と件数が一致(`menu-entry-ids`, `routine-refs-menus`, `menu-count`)
- サイズ: index 512 KiB、各 entry 256 KiB を超えない(75% 超は WARN)(`size-limit`)
- entry ファイル: `schemaVersion` が整数で index の `minSchemaVersion` 以下(`schema-version`)。menu / routine / segment の必須項目と型はアプリの `BackupDocumentParser` と同じ(`menu-field`, `segment-field`, `routine-field`, `routine-items`)
- segment: id が menu 内で一意、`orderIndex` が 0 からの連番、`durationSeconds` が正の整数、`type` が 1(work)か 2(rest)(`segment-id`, `order-index`, `duration`, `segment-type`)
- 図解: work の `illustrationId` が `known-pose-ids.txt` に存在、rest には付けない(`illustration-id`, `illustration-on-rest`)
- id の一意性: segment / menu / routine / routine item の id がカタログ全体で重複しない(`translationOf` で結ばれた翻訳版どうしは同じ id を共有してよい)(`duplicate-*-id`)
- `estimatedSeconds`: アプリの所要時間計算(準備 + sets × segment 合計 + (sets-1) × セット間休憩 + クールダウン、routine は各 item の `setsOverride ?? sets` で合算)と一致。routine は構成 menu エントリの合計とも一致(`estimated-seconds`)
- routine 内の menu が単体エントリと同内容(name・id・日時以外)(`inline-menu-mismatch`, `routine-menu-mismatch`)

WARN(ビルドは失敗しない):

- 日本語エントリの work segment の `description` が 40 文字超(実行画面で 2 行に収まらず切れる)(`ja-description-length`)
- title の「N分 / N min」が `estimatedSeconds` の四捨五入(0.5 は切り上げ)と一致しない(`title-minutes`)
- 既存エントリにないタグ(`tag-vocabulary`)
- 1 セットの menu が work 以外で終わる(`last-segment`)、listing 順が orderIndex 順でない、index から参照されない entries ファイル、など

文言や医療表現の妥当性は検査しない。

## known-pose-ids.txt の更新

アプリ(Intervalian)の `kPoseArt` にポーズが増減したら更新する。**必ずリリース済みの `origin/main` から生成すること。** フィーチャーブランチから作ると、インストール済みアプリが知らない id が lint を通ってしまい、公開後にそのポーズだけ図が出なくなる。アプリのリポジトリで:

```sh
git fetch origin main
git show origin/main:lib/core/illustrations/pose_art_data.dart | grep -o "^    id: '[^']*'" | sed "s/.*'\(.*\)'/\1/"
```

の出力を、先頭の `#` コメント行を残して `known-pose-ids.txt` に貼り替える(`#` で始まる行と空行は無視される)。
アプリより新しいポーズ id をカタログに入れても、古いアプリでは図が表示されない点に注意。

## ルールを追加するには

1. `lint_catalog.py` に検査を足し、`report.error(file, "ルール名", "内容")`(失敗させない場合は `report.warn`)で報告する。根拠にしたアプリ側のコードをモジュール冒頭の docstring かコメントに残す。
2. `test_lint_catalog.py` に、実カタログを一時ディレクトリへコピーして 1 か所だけ壊す異常系テスト(`red_green` ヘルパー: 壊す → exit 1 とメッセージを確認 → 元に戻す → exit 0 を確認)を足す。
3. 実カタログに対して `python3 -B intervalian/catalog/tools/lint_catalog.py` がエラー 0 のままであることを確認する。

## 公開の流れ

GitHub Pages は Actions 経由でデプロイされる(`.github/workflows/pages.yml`)。

1. main に push すると、まず `catalog-lint` ジョブ(このツールの lint とテスト)が走る。
2. lint が通ったときだけ `deploy` ジョブがリポジトリ全体(`.git` / `.github` / ドットファイルを除く)をそのまま公開する。
3. **lint が失敗すると、カタログ以外(offlink / ecoloop / intervalist のマニュアル等)を含めて、その push は一切公開されない。** 直前の公開版がそのまま残るので、lint を直して再度 push するまで更新は反映されない(意図した動作)。
4. 実行状況は GitHub の Actions タブ(workflow 名 `pages`)で確認する。失敗したジョブのログに `ERROR <ファイル>: [ルール名] ...` が出る。
5. pull request ではカタログ関連のファイルを変更したときだけ lint が走り、デプロイはされない。
6. 手動で再デプロイしたいときは Actions タブから `pages` を `Run workflow` で実行する(main で実行すること)。
