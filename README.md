# copper-golem 🟫🤖

Minecraft の**銅ゴーレム**（落ちたアイテムを拾い、同じ物が入ったチェストへ仕分ける）を、ファイル整理に翻訳した macOS 向けツール。

監視フォルダの直下に置かれたファイルを、**AI（Claude）が中身を読んで**、同じ階層にあるカテゴリフォルダへ自動で振り分けます。どこにも合わなければ**動かさず残します**（銅ゴーレム忠実）。

```
~/Downloads/                  ← 監視ルート（設定で変更可・複数可）
  請求書_2026.pdf  ←ここに置く  = 分類対象
  請求書/   .golem.md          ┐
  契約書/   .golem.md          ├ 同じ階層の兄弟フォルダ = 振り分け先
  写真/     .golem.md          ┘
  _未分類/                     ← 該当なしの退避先（任意）
```

## 特長

- **中身で分類**：拡張子だけでなく、PDF・Word・テキストの中身を読んで判断
- **AIインフラ不要**：`claude` CLI（Claude Code）をそのまま「分類器」に使う。**APIキー不要**（サブスク認証でOK）
- **即時・常駐**：ファイルを置いた瞬間に振り分け（macOS 標準の launchd `WatchPaths`、追加依存なし）
- **安全第一**：dry-run がデフォルト・移動ログ・`undo`・未完了ダウンロード除外・上書きせず連番リネーム
- **依存ゼロ**：Python 3.11+ の標準ライブラリのみ（設定は TOML）

## 仕組み

スクリプトがファイルの中身を抽出し、候補フォルダの説明と一緒に `claude -p` へ渡して、最適なフォルダを JSON で答えさせます。**Claude にファイルを触らせません**——抽出も移動もこのスクリプトが行うので、速く・安全です。

```
ファイル中身を抽出 ─┐
                    ├─▶ claude -p (Haiku) ─▶ {"folder": "...", "confidence": 0.95}
候補フォルダの説明 ─┘                              │
                                                  ▼
                              confidence が閾値以上なら mv（ログ記録）／未満なら残す
```

## 必要なもの

- macOS
- **[Claude Code](https://claude.com/claude-code)（`claude` CLI）がインストール済み・ログイン済み**（サブスクでOK）
- Python 3.11 以上
- **（推奨）`terminal-notifier`**（`brew install terminal-notifier`）— 常駐からの結果通知に使用。新しめの macOS では launchd からの `osascript` 通知が弾かれるため、これが無いと常駐時に通知が出ないことがあります（無くてもログには記録されます）
- （任意）`pdftotext`・`tesseract` があれば PDF/画像 OCR も扱える（無くても劣化動作）

## インストール

```bash
git clone <this-repo> copper-golem
cd copper-golem

# 設定を用意
mkdir -p ~/.config/copper-golem
cp config.example.toml ~/.config/copper-golem/config.toml
# ~/.config/copper-golem/config.toml を編集（watch_roots など）
```

## 設定（`~/.config/copper-golem/config.toml`）

`config.example.toml` を参照。主な項目：

| キー | 意味 |
|---|---|
| `watch_roots` | 監視するフォルダ（複数可）。直下のファイルが対象 |
| `model` | 分類に使うモデル。`claude-haiku-4-5`（最安・最速）推奨 |
| `dry_run` | `true` の間は移動せず判定だけ表示。慣れたら `false` |
| `on_no_match` | `"keep"`（残す）か、退避先フォルダ名（例 `"_未分類"`） |
| `confidence_threshold` | この確信度未満は「該当なし」扱い（誤分類を抑制） |
| `stability_seconds` | 直近 N 秒以内に更新されたファイルは飛ばす（DL中対策） |

**振り分け先フォルダは自分で作ります。** 監視ルート直下に `請求書/` `写真/` などを用意してください。フォルダに `.golem.md` を置くと、その説明文が分類のヒントになります（無くても中のファイル名から推測します）。

### `.golem.md` を自動生成する

各フォルダにすでに入っているファイルの内容から、「何を入れる場所か」の説明を AI に書かせて `.golem.md` として保存できます。

```bash
python3 golem.py describe --dry-run    # 生成内容を確認（書き込まない）
python3 golem.py describe --apply      # 各カテゴリフォルダに .golem.md を書く
python3 golem.py describe --apply --force   # 既存の .golem.md も上書き
```

既に `.golem.md` があるフォルダ・空のフォルダはスキップします。生成後は内容を見て、必要なら手で書き換えてください（「〜は含めない」などの除外条件を足すと精度が上がります）。

## 使い方

### まず手動で試す（推奨）

```bash
python3 golem.py --dry-run            # 設定の watch_roots を判定だけ表示
python3 golem.py --dry-run --root ~/golem-test   # ルートを一時上書き
```

出力例：

```
[dry-run] invoice_acme.txt  ->  invoices/  (conf=1.00)
[dry-run] carbonara.txt     ->  recipes/   (conf=0.99)
[keep]    random_note.txt   (conf=0.05, どのフォルダにも該当しない)
```

納得したら本番：

```bash
python3 golem.py --apply              # 実際に移動（config が dry_run=false でも可）
```

### 常駐させる（即時・自動）

```bash
./install.sh                          # launchd に登録（置いた瞬間に振り分け）
# 別の設定で：  ./install.sh --config ~/my-config.toml
```

ログは `~/Library/Logs/copper-golem.log`。**最初は `dry_run = true` のまま様子を見て**、ログが期待通りなら `dry_run = false` に変えて保存するだけ。**再 `./install.sh` は不要**で、常駐は実行のたびに設定を読み直します。

## 取り消し（undo）

直前のバッチをまとめて元の場所に戻します。

```bash
python3 golem.py undo
```

移動ログは `~/.local/state/copper-golem/moves.jsonl`。

## アンインストール

```bash
./uninstall.sh
```

## 安全について

- **dry-run がデフォルト**。実移動は `--apply` か設定の `dry_run=false` のときだけ
- 同名衝突は**上書きせず** `name (2).ext` にリネーム
- 監視ルート直下の**ファイルのみ**が対象（サブフォルダは動かさない）
- `.crdownload` などの**未完了ダウンロードは除外**、書き込み中のファイルは安定するまで待つ
- 全移動を JSONL に記録し、`undo` で戻せる

## 開発・テスト

外部依存なしのユニットテスト（`claude` 呼び出し・launchd・通知はモック）が付属します。

```bash
python3 -m unittest discover -s tests
```

## ライセンス

MIT（`LICENSE`）。著作権者名は適宜書き換えてください。
