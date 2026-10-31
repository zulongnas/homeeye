# go2rtc（HomeEye 定制版）怎么来

HomeEye 的 Docker 部署默认用**官方** `alexxit/go2rtc:master-hardware` 就能跑：
拉流、硬件转码、录像源都在官方能力里。

只有两件事需要**打过补丁**的 go2rtc：小米云台控制与语音对讲。它们走 Mi Home 的
`miss` 私有命令，官方不开放，所以我们在 upstream `v1.9.15` 上打了 15 个文件的补丁
（见 `PATCHES.md`，逐条写了为什么必须打）。

go2rtc 是 MIT 许可（`LICENSE`，Copyright (c) 2022 Alexey Khit）。本目录只放
**补丁文件**与说明，不复制上游源码：

```sh
# 一键做出镜像（默认 tag 就是 compose 引用的那个，跟着 go2rtc 的上游版本走）
#   ghcr.io/zulongnas/homeeye-go2rtc-ptz:<上游版本>
sh tools/build-go2rtc-ptz.sh
```

脚本自己下载 upstream tarball、校验 sha256、`git apply homeeye-ptz.patch`、
多阶段构建。改动的全部来源就是本目录的 `homeeye-ptz.patch`。
