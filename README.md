# tms_if_input

情報流通IFの **GeoJSONと施工計画XML** を、ROS2-TMS for Construction向けのパラメータと機械別BehaviorTreeに変換し、MongoDBに登録するROS 2パッケージです。

入力ファイル名、機械のエイリアス、地点・区間・タスクのID、タスク数、頂点数、ブロック数は固定していません。付属の `261001-kyoto.geojson` と `261001-kyoto.xml` は入力の一例です。別のGeoJSON/XMLをパスで指定するか、指定済みファイルの内容を差し替えて使用します。

**MongoDBへの書き込みは未登録文書の追加だけです。既存文書の削除・置換・上書き、既存同期フラグの初期化は行いません。** 同じキーの文書があれば、入力値が変わっていても既存の内容を保持します。

## 1. 実行手順

### 1.1 環境とビルド

検証環境はROS 2 Humble、Python 3.10、MongoDB 6.0です。ROS実行時は `rclpy`、`std_srvs`、`ament_index_python`、`launch`、`launch_ros`、`pymongo` を使用します。ROSを使わない変換だけなら、Python環境から実行できます。テストには `pytest` が必要です。

以下は、このリポジトリを `~/SIP_PROJECTS/ros2_tms_ws/src/tms_if_input` に配置した場合の手順です。別の場所に配置している場合は、ワークスペースのパスを変更してください。

```bash
source /opt/ros/humble/setup.bash
cd ~/SIP_PROJECTS/ros2_tms_ws
rosdep install --from-paths src/tms_if_input --ignore-src -r -y
colcon build --packages-select tms_if_input
source install/setup.bash
```

コードや付属サンプルを変更した場合は、再ビルドしてから `install/setup.bash` を読み込み直してください。新しいターミナルでもROS環境とワークスペース環境を読み込みます。

### 1.2 DB登録前に変換結果を確認する

ワークスペースのルートで、付属サンプルを変換します。このコマンドはMongoDBに接続しません。

```bash
ros2 run tms_if_input compile_scenario \
  --geojson src/tms_if_input/json_samples/261001-kyoto.geojson \
  --xml src/tms_if_input/json_samples/261001-kyoto.xml \
  --output-dir /tmp/tms_if_input-review \
  --dry-run
```

ROSを使わず、パッケージのソースディレクトリから実行することもできます。

```bash
cd ~/SIP_PROJECTS/ros2_tms_ws/src/tms_if_input
python3 -m tms_if_input.cli \
  --geojson json_samples/261001-kyoto.geojson \
  --xml json_samples/261001-kyoto.xml \
  --output-dir /tmp/tms_if_input-review \
  --dry-run
```

独自の施工計画を使う場合は、`--geojson` と `--xml` をそのファイルのパスに変更します。ファイルをサンプル名に改名する必要はありません。

出力先に `parameters.json`、`tasks.json`、機械ごとの `<model_name>_task.xml` が生成されます。付属サンプルでは65件のパラメータと4機械分のタスクを生成し、パラメータには18区間の運搬経路（幹線12・離合6）が含まれます。この件数はサンプル固有です。

`--dry-run` でも出力ファイルは更新されます。出力ディレクトリの扱いは「4.1 ファイル出力」を参照してください。

### 1.3 MongoDBに登録する

MongoDBを起動します。以下は `mongod` のsystemdサービスを使用する環境の例です。

```bash
sudo systemctl start mongod
```

パスを指定してROSノードを起動すると、起動時に1回、検証・変換・未登録文書の追加を行います。

```bash
ros2 launch tms_if_input tms_if_input.launch.py \
  geojson_path:=/absolute/path/to/plan.geojson \
  xml_path:=/absolute/path/to/plan.xml \
  output_dir:=/tmp/tms_if_input
```

入力パスを省略すると、インストール済みの付属サンプルを使用します。

```bash
ros2 launch tms_if_input tms_if_input.launch.py
```

起動時の自動登録後もノードは動作し続け、再読み込みサービスを提供します。`Ctrl+C` で終了できます。このノードは施工機械の動作を開始しません。

CLIで1回だけ登録する場合は、次のように `--dry-run` を付けずに実行します。既定の接続先は `mongodb://localhost:27017`、DBは `rostmsdb` です。

```bash
ros2 run tms_if_input compile_scenario \
  --geojson /absolute/path/to/plan.geojson \
  --xml /absolute/path/to/plan.xml \
  --output-dir /tmp/tms_if_input \
  --mongo-uri mongodb://localhost:27017 \
  --mongo-db rostmsdb
```

### 1.4 入力ファイルを差し替えて再読み込みする

起動時に指定したGeoJSON/XMLを差し替えた後、次のサービスを呼び出します。

```bash
ros2 service call /tms_if_input/scenario_importer/import std_srvs/srv/Trigger '{}'
```

呼び出しのたびに現在のファイルを読み込み、検証とファイル出力を行い、未登録のDB文書だけを追加します。成功時は `success: true` と追加件数・既存文書のスキップ件数を返し、失敗時は `success: false` と理由を返します。

**ファイルの差し替えは、既存DB文書の更新にはなりません。** 同じ `record_name`・対象モデルのパラメータ、同じ対象モデルのタスク、既存同期フラグは保持します。出力ファイルには新しい入力の変換結果が入るため、DBの既存内容とは異なる場合があります。

別パスのファイルを使う場合は、そのパスを指定してlaunchを起動し直すか、CLIで実行してください。サービス呼び出しでファイルパスを渡す形式ではありません。

起動時の処理を待機させるには `import_on_start:=false`、DB登録を省略して変換だけ行うには `dry_run:=true` を指定します。`dry_run` は再読み込みサービスにも適用されます。

```bash
ros2 launch tms_if_input tms_if_input.launch.py \
  geojson_path:=/absolute/path/to/plan.geojson \
  xml_path:=/absolute/path/to/plan.xml \
  import_on_start:=false \
  dry_run:=true
```

## 2. 入力仕様

両ファイルはUTF-8で読み込み、BOM付きUTF-8にも対応します。IDは整数または空でない文字列を使い、内部では文字列として照合します。サンプルにある数値IDや機械名を処理条件に埋め込んでいません。

### 2.1 GeoJSON

ルートは `FeatureCollection`、`features` はFeatureの配列です。

| 用途 | geometry.type | 主なフィールド | 条件 |
| --- | --- | --- | --- |
| 地点 | `Point` | `properties.id`、`geometry.coordinates: [x, y]` | 地点IDでXMLから参照する。IDはFeature直下の `id` でも指定可能 |
| 地点の経路種別 | `Point` のメタデータ | `properties.metadata.route_rank` | 整数の `0` は幹線、`1` は離合。省略時は `0` |
| 区間 | `LineString` | `properties.id`、`startid`、`endid`、座標配列 | 端点IDは存在するPointを参照し、座標配列は2点以上 |
| 走行可能エリアの境界 | `Polygon` | `properties.name: "geo_fence"`、リング配列 | 各リングは4点以上で、始点と終点が一致すること |

PointとLineStringのIDは、GeoJSON内で重複させないでください。LineStringの最初と最後のXYは、それぞれ `startid` と `endid` のPointと一致させます。入力の丸め誤差として、各座標の絶対差 `0.001` までを許容します。

座標系の変換は行わず、入力の最初の2成分をXYとして使用します。地点・経路のナビゲーション用Zは `0.0` を保存し、XMLのブロック中心は入力のXYZをそのまま保存します。

ポリゴンのリング数・頂点数、LineStringの途中の頂点数は可変です。`geo_fence` は全リングを保存しますが、このパッケージ自体は走行可否判定や経路の衝突判定を行いません。

### 2.2 XMLの構造とタスク

施工計画XMLは、次の3部分で構成します。

| 部分 | 内容 |
| --- | --- |
| `ConstructionPlan/machines/machine` | `id` はタスクから参照するエイリアス、`type` はDBと実行ツリーで使うモデル名 |
| `ConstructionPlan/procedure/tasks/task` | `id` はタスクID、`name` はタスク種別。各パラメータは `<parameter name="..." value="..." />` で指定 |
| `ConstructionPlan/procedure/flow/edge` | `from` と `to` にタスクIDを指定し、先行タスクから後続タスクへの依存関係を表す |

使用できるタスク種別は以下の6種類です。`start` と `end` 以外は `machine` に宣言済み機械のエイリアスを指定します。

| タスク種別 | 動作 | 必須パラメータ（`machine` 以外） |
| --- | --- | --- |
| `start` | 施工開始 | なし |
| `initialize` | 機械の初期配置・姿勢設定 | `target_node`、`rotation`、機種に応じた関節パラメータ |
| `excavation_loading` | 掘削機による掘削・積載 | `block_size`、`block_angle`、`block_center`、`block_vector`、`backhoe_node`、`dump_node` |
| `transport` | ダンプによる運搬・離合 | `excavation_loading_task`、`leveling_task` |
| `leveling` | ブルドーザによる敷き均し | `leveling_height`、`block_size`、`block_center`、`block_vector`、`soil_volume`、`dump_node`、`bulldozer_node`、`connection_node` |
| `end` | 施工終了 | なし |

パラメータの値は、数値、ID、配列、ベクトルを使用します。以下のような引用符なしのキーを持つベクトル表記にも対応します。コードとして評価する処理は行いません。

```xml
<parameter name="rotation" value="{x:0.0, y:0.0, z:0.0, w:1.0}" />
<parameter name="block_vector" value="{x:1.0, y:0.0, z:0.0}" />
<parameter name="dump_node" value="[101, 102, 103]" />
```

主な値と配列の条件は次のとおりです。

| 対象 | 条件 |
| --- | --- |
| `initialize.rotation` | 数値の `x/y/z/w` を指定。全成分ゼロの四元数は不可 |
| 掘削機の初期関節 | `swing`、`boom`、`arm`、`bucket` のうち1つ以上を指定。指定した値を対応する関節へ保存 |
| ダンプの初期関節 | `swing` と `vessel` の両方を指定 |
| 掘削の `block_size` | 正の `x/y/z` |
| 敷き均しの `block_size` | 正の `x/y` |
| `block_vector` | 数値の `x/y/z` を指定。XY方向のゼロベクトルは不可 |
| `block_center` | XYZベクトルの空でない配列 |
| 掘削の配列 | `block_center`、`backhoe_node`、`dump_node` の長さを一致させる |
| 敷き均しの配列 | `block_center`、`dump_node`、`bulldozer_node`、`soil_volume` の長さを一致させる |
| `soil_volume` | 各要素は0以上の有限数 |
| `connection_node` | 1点以上のPoint ID配列。偶数個・奇数個とも使用可能 |
| `transport` のタスク参照 | `excavation_loading_task` は掘削積載タスク、`leveling_task` は敷き均しタスクのID |

位置の参照先はすべてGeoJSONのPoint IDです。数値パラメータは有限値を使用し、関節角度やブロック角度の値は単位変換せず保存します。

`start` と `end` はそれぞれ1つ必要です。全タスクがstartから到達でき、endへ到達する循環のないフローにします。作業の前には同じ機械のinitializeが必要です。同じ機械のタスクを並列に配置するフローは受け付けません。

機械のエイリアスとモデル名は、それぞれ重複させないでください。同型式を複数台使用する場合は、`type="zx200_1"`、`type="zx200_2"` のように一意のインスタンス名を使用します。

既知の型式は `zx120` / `zx200`（掘削機）、`ic120` / `mst110cr` / `mst2200vdr`（ダンプ）、`d37pxi`（ブルドーザ）です。別型式でもタスクの役割や初期関節から機械種別を判定しますが、判定できない場合や種別が矛盾する場合はエラーにします。モデル名は入力の表記を保持します。

## 3. 変換仕様

### 3.1 パラメータ

以下の文書を `rostmsdb.parameter` 向けに生成します。番号 `n` はXML配列の順序に従う1始まりの番号です。

| `record_name` | 対象モデル | 主な内容 |
| --- | --- | --- |
| `geo_fence` | 指定なし | `type: static`、`coordinates: [[{x, y}, ...], ...]`。全リングの頂点を保存 |
| `initial_position` | 対象機械の文字列 | `type: static`、XY位置、`z: 0`、XMLの四元数 `qx/qy/qz/qw` |
| `initial_pose`（掘削機） | 対象機械の文字列 | `planning_group: manipulator`、`joint_values_relative` のwaypoints。入力関節名に `_joint` を付けて保存 |
| `initial_move_pose`（掘削機） | 対象機械の文字列 | 掘削機の姿勢レコード形式で `swing_joint: 0.0` を保存 |
| `initial_pose`（ダンプ） | 対象機械1台の配列 | `type: static`、`target_angle: swing` |
| `initial_pose_vessel` | 対象機械1台の配列 | `type: dynamic`、`target_angle: vessel` |
| `initial_move_pose`（ダンプ） | 対象機械1台の配列 | `type: static`、`target_angle: 0.0` |
| `excavation_loading_params` | 掘削機の文字列 | `type: dynamic`、`task_type: excavation_loading`、XMLのブロック情報と、座標に解決した `backhoe_node` / `dump_node` |
| `loading_position_n` | 宣言された全ダンプの配列 | `type: static`、積載点のXY、Z=0、未提供の四元数は各成分 `"[dummy]"` |
| `dumps_entry_point_leveling_area` | 全ダンプの配列 | `type: static`、敷き均しエリアの入口位置と姿勢 |
| `dump_node_n` | 全ダンプの配列 | `type: static`、対応する経由点と放土点の2点分の `x/y/z/qx/qy/qz/qw` 配列 |
| `bulldozer_node_n` | ブルドーザの文字列 | `type: static`、ブルドーザの移動位置と姿勢 |
| `block_n` | 指定なし | `type: static`、`block_size`、該当する `block_center`、`soil_volume` |
| 区間ID | 全ダンプの配列 | 運搬経路の座標配列、区間種別、前後リンク。「3.2 運搬経路」を参照 |
| `initialize_flgs` | 指定なし | 機械・初期化タスクごとの完了フラグ |
| `task_completion_flgs` | 指定なし | 後続タスクから完了を参照される作業タスクのフラグ。必要な場合だけ生成 |

掘削機の姿勢レコードは `time_scale`、`acceleration_scale`、`velocity_scale` をそれぞれ `1` とします。掘削積載パラメータの地点配列は `[{x, y}, ...]` として保存し、ブロック中心のXYZはXMLの値を保持します。

敷き均しの入口は `connection_node` 配列の中央要素です。要素数をNとすると、0始まりの添字は `(N - 1) // 2` です。3点なら2番目、4点なら2番目を選びます。

放土地点は、`block_vector` と平行に並ぶ列ごとに経由点と対応付けます。各経由点を通る平行線への横方向距離が最小のものを選び、同距離で判別できない場合はエラーにします。ダンプの向きは `-block_vector`、ブルドーザの向きは `+block_vector` のXY方向からyawを求め、四元数へ変換します。

同じ機械で同じ種類のタスクを繰り返し、レコード名が衝突する場合は `_<task ID>` を付けます。共有レコードの `loading_position_n`、`dump_node_n`、`block_n`、敷き均し入口は、別機械の同種タスクとの衝突も区別します。

複数の敷き均しタスクでは `leveling_params`、複数の作業エリアを参照する運搬や同一機械の複数運搬タスクでは `transport_params` を追加します。これらには参照するパラメータ名、関連タスクID、経路区間IDなどを保存し、実行ツリーから参照します。単一エリアの `leveling_height` は検証しますが、独立したレコードには出力しません。

### 3.2 運搬経路

経路はGeoJSONの接続関係と、transportが参照する掘削積載・敷き均しタスクから構築します。

1. 掘削積載・敷き均しタスクで使用する地点をPoint集合から除き、運搬用地点を抽出する。initializeの参照だけを理由に地点を除外しない。
2. 端点の少なくとも一方が運搬用地点であるLineStringを抽出する。
3. `startid` / `endid` が逆になっている重複区間は、一方だけを残す。
4. 端点のいずれかが `route_rank=1` なら `sub`、それ以外は `main` とする。
5. 積載側から敷き均し側への向きを `up`、逆を `down` として、必要に応じて全頂点の順序を反転する。
6. 幹線・離合それぞれの前後区間をIDで関連付ける。各transportについて、参照する積載エリアから敷き均しエリアへの接続を検証する。

区間文書は `type: static`、`record_name` と `section_id` は区間IDの文字列です。座標・姿勢は全頂点分の `x/y/z/qx/qy/qz/qw` 配列で、Z=0、四元数は `[0, 0, 0, 1]` を各頂点に保存します。

| フィールド | 内容 |
| --- | --- |
| `label` | `main` または `sub` |
| `preferred_direction` | `up` |
| `related_point_up_main` / `related_point_up_sub` | up側の幹線・離合区間ID |
| `related_point_down_main` / `related_point_down_sub` | down側の幹線・離合区間ID |

関連区間がない場合は空文字列を保存します。敷き均しの複数経由点へ向かう末端分岐では全枝を保存し、各枝から前の幹線へのリンクを保存します。分岐直前の幹線は、単一の継続区間を指定できないためup側の幹線リンクを空文字列とします。

対応する幹線は、連結成分ごとに積載側の端点を1つ持つ木構造です。離合路は同じ幹線内の2地点を結ぶ分岐のない経路とし、方向を決められる位置関係が必要です。幹線の循環、無関係な地点で終わる経路、離合路の分岐、単一の前後リンクで表せないその他の分岐はエラーにします。

### 3.3 機械別タスクと同期

初期化を含む動作タスクを持つ機械ごとに1件のBehaviorTree文書を `rostmsdb.task` 向けに生成します。文書には `task_id`、`type: "task"`、`model_name`、`description: "<model_name>_task.xml"`、XML文字列の `task_sequence` を含めます。

XMLのflowを解釈して実行順序を決め、各タスクはすべての先行動作タスクの完了を待ってから1回実行します。掘削機・ダンプ・ブルドーザそれぞれのLeafNodeと初期化プリミティブを生成します。

同期フラグの初期値は `false` とし、実行ツリーで動作完了後に `true` を書き込みます。後続機械が完了を取りこぼさないよう、生成ツリーは完了フラグを途中で下げません。既存のフラグ文書は、再登録時も内容を保持します。

生成LeafNodeは資料の `primitive_name` インターフェースを使用します。このワークスペースにある従来のLeafNodeは `subtask_name` インターフェースのため、施工実行には資料のポートと高位タスク（`excavation_loading` / `transport` / `leveling`）に対応した実行側が必要です。本パッケージの担当範囲は変換・ファイル出力・DBへの追加です。

## 4. 出力とMongoDB登録の仕様

### 4.1 ファイル出力

| 出力ファイル | 内容 |
| --- | --- |
| `parameters.json` | 現在の入力から生成したパラメータ文書の配列 |
| `tasks.json` | 現在の入力から生成した機械別タスク文書の配列 |
| `<model_name>_task.xml` | 各タスク文書の `task_sequence` と同じBehaviorTree XML |

既定の出力先は `/tmp/tms_if_input` です。入力の検証・変換が成功した後にファイルを書き込み、その後でDBへ接続します。DB接続に失敗しても、変換済みファイルが残る場合があります。

同じ出力先で再実行するとJSONと現在の機械のXMLを更新します。以前の `tasks.json` に記載された不要XMLは、内容が前回の生成結果と一致する場合だけ削除します。ユーザーが編集した不要XMLと、その他のファイルは保持します。**この出力ファイルの更新・整理と、既存MongoDB文書を保持する仕様は別です。**

### 4.2 登録先と既存文書の照合

DB名は `mongo_db` / `--mongo-db` で変更できます。コレクション名は `parameter` と `task` です。

| コレクション | 既存文書とみなすキー | 既存文書がある場合 |
| --- | --- | --- |
| `parameter` | `record_name` と `model_name` の値 | 既存文書をそのまま保持し、追加をスキップ |
| `task` | `type: "task"` と `model_name` | 既存文書と `task_id` を保持し、追加をスキップ |

パラメータの `model_name` は、文字列・配列・フィールドなしを区別します。配列は要素の順序も含めて照合します。たとえば `"mst110cr"` と `["mst110cr"]` は別の対象です。共有パラメータの対象機械配列が変わった場合は、新しい照合キーとして追加します。

入力に含まれなくなった文書、前回登録した文書、手作業で格納した文書、同期フラグも削除・更新しません。`import_key` は新規文書の `_tms_if_input.import_key` に記録する出典ラベルで、既存文書の操作範囲を指定するものではありません。

新規タスクの `task_id` は、taskコレクションで未使用の最小の正整数を割り当てます。出力ファイル内のIDは変換時の機械の並び順に基づくため、DBに保存するIDと異なる場合があります。実際に登録・参照したIDはCLIの `task_ids` で確認できます。

### 4.3 登録結果と失敗時の扱い

CLIは成功時に結果をJSONで標準出力し、終了コード `0` を返します。変換・出力・DB処理の失敗時は理由を標準エラー出力し、終了コード `1` を返します。必須引数の不足や不正な引数では、終了コード `2` を返します。

| 報告フィールド | 意味 |
| --- | --- |
| `parameter_count` / `task_count` | 今回の入力から生成した件数 |
| `inserted_parameters` / `inserted_tasks` | 新しくDBに追加した件数 |
| `skipped_parameters` / `skipped_tasks` | 同じキーの既存文書があったため保持した件数 |
| `task_ids` | 対象モデル名とDB側のタスクIDの対応 |
| `output_dir` / `dry_run` | 出力先と変換のみの実行かどうか |

`dry_run` の場合はDB登録用の件数と `task_ids` を報告しません。ROSノードは追加・スキップ件数と出力先をログ、およびサービス応答で報告します。

不正な入力はDBへの追加前に拒否します。既存task文書の正整数IDの重複、対象モデルのタスク文書の重複、対象モデルの不正なタスクIDも追加前にエラーにします。これらの既存文書を自動修正する処理はありません。

DBエラー時は一部の新規文書だけが追加済みになる場合があります。同じ入力で再実行すると、追加済み文書を保持して残りの未登録文書を追加します。登録処理は直列に実行してください。同時実行による重複を防ぐ処理は実装していません。

## 5. 設定項目

launchでは `名前:=値`、CLIでは対応する `--オプション` で指定します。

| launch引数・ROSパラメータ | CLIオプション | 既定値 | 内容 |
| --- | --- | --- | --- |
| `geojson_path` | `--geojson` | launchは付属GeoJSON | 入力GeoJSON。CLIでは必須 |
| `xml_path` | `--xml` | launchは付属XML | 入力XML。CLIでは必須 |
| `output_dir` | `--output-dir` | `/tmp/tms_if_input` | ファイル出力先 |
| `mongo_uri` | `--mongo-uri` | `mongodb://localhost:27017` | MongoDB接続URI |
| `mongo_db` | `--mongo-db` | `rostmsdb` | DB名 |
| `mongo_timeout_ms` | `--mongo-timeout-ms` | `5000` | サーバー選択・接続・ソケットの各タイムアウト。正整数、単位ms |
| `import_key` | `--import-key` | `default` | 新規文書の出典ラベル。空文字列・空白のみは不可 |
| `dry_run` | `--dry-run` | `false` | 検証・ファイル出力のみ。DBには接続しない |
| `import_on_start` | なし | `true` | ROSノード起動時に1回処理する |
| `prefix`（launchのみ） | なし | `tms_if_input` | 使用するROS名前空間 |
| `use_namespace`（launchのみ） | なし | `true` | `prefix` の名前空間を使うか |
| `use_sim_time` | なし | `false` | ROSのシミュレーション時刻を使うか |

ノードを `ros2 run tms_if_input scenario_importer` で直接起動する場合、入力パスの既定値は空なので、両方をROSパラメータで指定してください。CLIは1回実行して終了し、再読み込みサービスは提供しません。

サービスはノードのプライベート名 `~/import` です。既定の名前は `/tms_if_input/scenario_importer/import`、`use_namespace:=false` の場合は `/scenario_importer/import` です。`prefix` を変更した場合もサービス名の名前空間が変わります。

## 6. エラー確認と検証

| 症状 | 確認する内容 |
| --- | --- |
| 入力ファイルが見つからない | 指定パスと実行ディレクトリ。独自ファイルは絶対パスで指定すると確認しやすい |
| JSON/XMLの解析エラー | JSONの構文、XMLのタグ・属性、パラメータのベクトル・配列表記 |
| Point・機械・タスクの参照エラー | XMLのIDとGeoJSONのPoint ID、機械宣言、関連タスクIDの対応 |
| 配列長・フロー・経路のエラー | 「2. 入力仕様」「3.2 運搬経路」の条件 |
| MongoDBの接続エラー | MongoDBが起動しているか、URIと認証設定、必要に応じて `mongo_timeout_ms` |
| 再実行してもDBの値が変わらない | 同じ照合キーの既存文書を保持する仕様。追加件数とスキップ件数を確認 |
| 付属サンプルやコードの変更が反映されない | 再ビルドと `source install/setup.bash`。独自入力はlaunchでパスを明示 |

テストはソースディレクトリで実行します。

```bash
cd ~/SIP_PROJECTS/ros2_tms_ws/src/tms_if_input
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
```

入力ID・機械名の変更、座標の回転・平行移動、可変頂点数・ブロック数・接続点数、複数施工エリア、経路接続、機械間の同期、既存DB文書の保持、失敗後の再試行、ファイル出力を検証します。DBの回帰テストはメモリ上の代替実装を使い、実際のMongoDBには接続しません。

## 7. 実装ファイル

| ファイル | 担当 |
| --- | --- |
| [model.py](tms_if_input/model.py) | GeoJSON/XMLの読み込み、ID解決、入力検証 |
| [parameters.py](tms_if_input/parameters.py) | 初期化・掘削・敷き均しのパラメータ生成 |
| [routes.py](tms_if_input/routes.py) | 運搬・離合経路の抽出、向きと前後リンクの生成 |
| [behavior_tree.py](tms_if_input/behavior_tree.py) | 機械別実行ツリーと同期フラグの生成 |
| [compiler.py](tms_if_input/compiler.py) | 変換処理の統合、出力ファイルの生成 |
| [database.py](tms_if_input/database.py) | 既存文書を保持した未登録文書の追加 |
| [cli.py](tms_if_input/cli.py) | CLIの引数・結果報告 |
| [importer.py](tms_if_input/importer.py) | ROSノードと再読み込みサービス |
| [tms_if_input.launch.py](launch/tms_if_input.launch.py) | 起動引数、入力パス、名前空間の設定 |

旧JSONの `phase` / `machinery_tasks` を処理する5ノード構成は廃止し、DB登録を `scenario_importer` の1ノードにまとめています。

資料の例にある機械名・関節角度などの転記違い、途中で切れた経路一覧は固定値として採用せず、入力ファイルの値と変換規則を使います。付属GeoJSONの壊れていたFeature構造は、座標値を保持して構文を修正しています。
