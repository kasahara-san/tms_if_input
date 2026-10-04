# tms_if_input

GeoJSONと施工計画XMLから、施工機械ごとのタスクとパラメータを生成するROS 2パッケージです。
MongoDBの `rostmsdb.parameter` と `rostmsdb.task` に未登録データだけを追加し、既存データは削除・更新しません。

```bash
ros2 launch tms_if_input tms_if_input.launch.py \
  geojson_path:=261004-kyoto.geojson \
  xml_path:=261004-kyoto.xml
```

`geojson_path` と `xml_path` には、`json_samples` 直下のファイル名を指定します。省略すると上記のサンプルを使用します。
MongoDB（既定: `localhost:27017`）を起動してから実行してください。変換・登録後に自動終了し、変換結果は `/tmp/tms_if_input` に出力します。再読み込みサービスを常駐させる場合だけ `keep_alive:=true` を付けます。

入力ファイルやコードを変更した場合は、ワークスペースで `colcon build --packages-select tms_if_input` を実行し、`source install/setup.bash` で環境を読み込み直してから起動してください。
