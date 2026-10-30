# 小米 Miloco SDK 不在本仓库里

HomeEye 的小米摄像头取流靠 `miloco_sdk`（内含小米预编译原生库
`libmiot_camera_lite.so`）。那个包受 **Xiaomi Miloco License Agreement** 约束，
不是可自由再分发的开源许可，所以本仓库**不打包、不镜像**它 —— 装的人自己去上游取，
许可条款由取的人直接对小米负责。

不需要小米摄像头的话，这个目录空着就行，RTSP / ONVIF / 其他流一律照常工作
（`nw_xiaomi.xiaomi_available()` 探不到 SDK 只会把小米那一栏标成不可用，不影响别的路）。

要小米：

```sh
sh tools/fetch-miloco-sdk.sh          # 拉到 app/sdk/miloco-sdk-main
```

然后重新构建镜像（Dockerfile 里 `HE_WITH_MILOCO_SDK=1` 会顺带装齐 SDK 的 Python 依赖）。
