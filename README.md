# tms_if_input

GeoJSONと施工計画XMLからタスク・パラメータをMongoDBへ追加します。経路グラフ・敷き均しパラメータ・放土状態のROS 2トピック送信にも対応しています。

**ビルド**（入力ファイルやコードを変更した際も再実行）

```bash
cd ~/SIP_PROJECTS/ros2_tms_ws
source /opt/ros/humble/setup.bash
colcon build \
  --base-paths src/tms_if_input src/tms_if_input/additional_interfaces/machinery_task_msgs \
  --packages-select machinery_task_msgs tms_if_input
source install/setup.bash
```

MongoDB（既定: `localhost:27017`）を起動してください。

```bash
sudo systemctl start mongod
```

**MongoDBへの登録**

`parameter_name` に `json_samples` 直下の共通ファイル名を拡張子なしで指定します。`.geojson` と `.xml` が補完され、省略時は `261004-kyoto` です。

```bash
ros2 launch tms_if_input tms_if_input.launch.py \
  parameter_name:=261008-kyoto
```

`rostmsdb.parameter` と `rostmsdb.task` に未登録データを追加して終了します。既存データは削除・更新しません。

**追加トピック機能の起動**

```bash
ros2 launch tms_if_input system_topics.launch.py \
  geojson_path:=261008-kyoto.geojson \
  xml_path:=261008-kyoto.xml
```

受領応答に合わせて `NodeGroup` → `LevelingTaskParam` → `LevelingStateReport` の順に送信します。対応する `TaskFinish` で状態送信を停止し、ノードは `Ctrl+C` で終了します。トピック機能だけの検証では、上記のMongoDBへの登録は不要です。

別ターミナルで受領応答・完了通知を順に送ります。IDは `261008-kyoto.xml` の例です。入力を替えた場合は、敷き均しタスクIDと機械aliasに合わせて変更してください。

```bash
source ~/SIP_PROJECTS/ros2_tms_ws/install/setup.bash
ros2 topic pub --once /system/MessageReport machinery_task_msgs/msg/TaskReport \
  "{message_id: 'FirstSetGraph', task_id: '', machine_id: 'machine_4'}"
ros2 topic pub --once /system/MessageReport machinery_task_msgs/msg/TaskReport \
  "{message_id: 'TaskGet', task_id: '2600159710', machine_id: 'machine_4'}"
# 状態報告を確認したら完了を通知
ros2 topic pub --once /system/MessageReport machinery_task_msgs/msg/TaskReport \
  "{message_id: 'TaskFinish', task_id: '2600159710', machine_id: 'machine_4'}"
```

放土状態を試すには、`rostmsdb.parameter` に次の例を追加します。`RELEACED_BLOCK` は放土済みブロック名の配列です。

```json
{
  "model_name": "d37pxi",
  "type": "dynamic",
  "record_name": "SAMPLE_BLACKBOARD_d37pxi",
  "RELEACED_BLOCK": ["block_1", "block_2"]
}
```

`block_state` はXMLのブロック順に、放土済みなら `true`、それ以外は `false` です。
