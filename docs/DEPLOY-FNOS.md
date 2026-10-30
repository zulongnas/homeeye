# 飞牛 fnOS 安装与上架（.fpk）

## 装（用户视角）

**应用中心上架之后**：应用中心搜 HomeEye → 安装 → 图标栏打开。这是官方推荐的公开
分发方式。

**目前（未上架）**：只能手动安装 `.fpk`。注意官方文档明确写着手动安装
「仅限本地调试，不应作为公开分发方式」（[测试应用](https://developer.fnnas.com/docs/quick-started/test-application/)）。
所以本仓库 Release 里放的 `.fpk` 是给你自己装/验证用的；要正规模分发给别人，
请走下面的上架流程。

```sh
sh tools/pack-fpk.sh            # 产出 homeeye.fpk（用官方 fnpack 校验 + 注入 checksum）
```

装完打开是**零配置**：没有摄像头、没有米家账号、没有录像。安装向导只问一件事——
录像往哪个目录写（不知道选啥就留空，用飞牛给本应用开的共享目录）——第一件事仍然是设登录密码。

## 包结构（本仓库根目录就是 fnpack 的源目录）

```
manifest              包清单（见下）
ICON.PNG              64x64     审核项：尺寸必须是 64
ICON_256.PNG          256x256   PNG 或 JPG、sRGB、≤1024KB、完整正方形画布，
                                视觉主体须为圆角矩形（不得直角满铺）
app/server/*.py       后端        }
app/www/*             前端        } 打成 app.tgz，tgz 根 = app/ 的内容
app/bin/heptz         云台侧车    }
app/scripts/*.sh      生命周期脚本共用的助手（录像目录落地）
app/ui/config         桌面入口 + 统一网关注册
app/ui/images/*       入口图标
cmd/main              start/stop/status
cmd/{install,upgrade,uninstall}_{init,callback}   6 个钩子（必填）
cmd/{config_init,config_callback}                配置向导前后钩子
wizard/install        安装向导：选录像存放目录（field=wizard_rec_dir）
wizard/config         配置向导：改录像存放目录（同一个 field，留空=不改）
config/privilege      运行身份
config/resource       共享目录声明
```

`cmd/config_init`、`cmd/config_callback` 实测可缺省（本项目仍然带上：配置向导要靠
`config_callback` 把用户改的目录写进配置）。

## 安装时选录像目录（向导）

飞牛把向导里每个 `field` 的名字**原样**变成环境变量交给生命周期脚本，所以
`wizard/install` 里那个 `wizard_rec_dir` 就是 `cmd/install_callback` 读到的值。
落地的活儿在 `app/scripts/apply_storage_dir.sh`（安装与配置两个钩子共用一份，
两边不会漂），规则三条：

- **留空 = 默认目录**：`$TRIM_DATA_SHARE_PATHS` 第一项（飞牛为本应用开的共享目录，
  就是文件管理器里能看到 HomeEye 那个）下面的 `recordings/`；探不到就退回
  `$TRIM_PKGVAR/recordings`。装在哪个存储卷，录像就落在哪个卷。
- **写不进去不硬来**：`mkdir` 失败或不可写 → 记一条用户可见的说明，退回默认目录，
  绝不留下"设置成功、从此不长录像"的现场。
- **落盘到 `config.json` 的 `storage.dir`**，而不是只当环境变量留着：飞牛只在
  安装/配置那一次注入向导值，之后的 `start` 不一定还看得见；而 HomeEye 按 mtime
  重载配置，落盘的 `storage.dir` 服务立刻能用，设置页也能如实回显。

改地方的两处，都不用重装：

1. 应用中心 → 本应用 → **配置**（`wizard/config`）：填新目录保存，最迟下一个录像
   切片就写到新目录；留空表示不改。**老分段不搬** —— 把用户几十 GB 视频悄悄挪走
   这种事必须由人自己决定。
2. HomeEye 网页 → 设置 → 存储：同一件事，还带一个"本机可见目录 + 剩余空间"的选择器。

坑（社区实测，非官方）：向导 `text` 的 `min/max` 校验的是**字符串长度**，不是数值范围，
所以路径字段只用 `pattern` 校验格式（`^(|/...)$`：要么留空，要么绝对路径）。

## manifest 的坑（本项目踩过，别重复）

1. **manifest 不支持 `#` 注释。** 官方示例里没有注释语法，`#` 开头的行会被解析成
   垃圾键。要解释"为什么这么写"，写进本文件，不要写进 manifest。
2. **走统一网关 socket 时不要声明 `service_port`。** 本项目的服务只监听 unix socket
   （`app/ui/config` 里 `gatewaySocket=app.sock`），此时 `protocol`/`port` 被网关忽略；
   但如果留着 `service_port` 而 `checkport` 又没关，应用中心会去连一个不存在的端口，
   **一直卡在「启动中」**。本项目的写法是：不写 `service_port`，保留
   `checkport = false`。
3. `platform` 合法值实测为 `x86 | arm | loongarch | risc-v | all`（文档只列前三个）。
   本项目写 `x86` 是**诚实值**：`app/bin/heptz` 目前是 amd64 静态二进制，
   没在 ARM 上验证过。官方审核要求「不得声明超出实测的兼容范围」，
   所以别顺手改成 `all`。要支持 ARM，先重编 heptz 并实测。
4. `version` fnpack 不校验格式，但社区经验是 `X.Y.Z`；本项目跟 Docker 镜像 tag 一致。
5. `checksum` 由 fnpack 自动注入（`md5(app.tgz)`，32 位小写十六进制），**不要手写**。

键的语义见 [Manifest](https://developer.fnnas.com/docs/core-concepts/manifest/)。

## 生命周期脚本契约

`cmd/main` 收 `$1`：`start` / `stop` / `status`，其它参数 `exit 1`。
退出码：**0 = 成功**（`status` 时 = 正在运行）、**1 = 失败**、**3 = `status` 时未运行**。
见 [应用框架](https://developer.fnnas.com/docs/core-concepts/framework/)。

本项目的 `main` 有三条自己的规矩，改它之前先读懂：

- **`start` 必须快速返回**。飞牛等 `start` 结束才判定"启动完成"，所以只做
  后台拉起 + 最多 8 秒等 socket 出现，不做复杂 pid 反查。
- **`stop` 要收掉整棵进程树**。HomeEye 会 fork 一堆 ffmpeg 子进程；服务用 `setsid`
  自建会话，`stop` 按命令行精确匹配 `pkill -TERM` → 10 秒 → `-KILL` 兜底。
  留孤儿 ffmpeg 会一直占着摄像头的连接名额（小米每台只给 3 条 P2P 会话）。
- **禁止再降权**。`config/privilege` 里 `run-as: package` 已经让脚本以应用账号运行，
  再 `setpriv`/`su` 一次会把权限模型搞乱（官方也明示不建议 `run-as: root`）。

失败前把用户可读的错误写进 **`$TRIM_TEMP_LOGFILE`** —— 这是官方唯一的"用户可见报错"
通道，只写 `info.log` 用户在界面上看不到。

注入的环境变量（[环境变量](https://developer.fnnas.com/docs/core-concepts/environment-variables/)）
本项目用到：`TRIM_APPDEST`（默认 `/var/apps/homeeye/target`）、`TRIM_PKGVAR`
（默认 `/vol1/@appdata/homeeye`）、`TRIM_TEMP_LOGFILE`。

**幂等**：官方明示安装/升级/配置钩子可能被重复执行，本项目所有钩子可重入。

**脚本不需要自己做可执行位**。fnpack 在 Windows 上出的包里，`cmd/*` 与 `manifest`
的 tar 成员模式是 `0666`（没有 x 位），看着像缺陷。实测结论：1.0.4 的包在 NAS 上装完，
`/var/apps/homeeye/cmd/*` 全部是 `0755 root:root` —— **安装端自己补的执行权限**，
所以不用为这个改包（已核对：包内 0666 / 装后 0755）。

## 数据与卸载

- 配置、登录口令派生值、米家授权缓存、自签证书都在 `TRIM_PKGVAR`（`@appdata/homeeye`）里，
  跨版本由系统持有、升级不丢。**录像分段本体**按安装向导里选的目录落盘
  （默认是 `@appshare/homeeye` 下面的 `recordings/`），所以卸载重装后能不能接着看到老录像，
  取决于那个目录还在不在 —— 本项目刻意不删它。
- `config/resource` 声明了两个面向用户的共享目录（`homeeye`、`homeeye/data`），
  作用是让**录像能在飞牛文件管理器里直接看/拷**。这是刻意声明的能力，
  官方要求"面向用户的共享目录须在界面或更新说明中说明"—— `manifest` 的 `changelog`
  与本文件都说明了。
- 卸载：`uninstall_init` 只停服务，**不删录像**。官方只要求"卸载逻辑应尊重用户数据"，
  没有"必须删干净"的成文条款；把 500GB 录像在一次卸载里悄悄抹掉是不可逆的事故，
  所以本项目选择保留。要彻底清理，卸载后手工删 `@appdata/homeeye` 与共享目录里的
  `homeeye/data`。

## 上架审核清单

官方公开的上架要求见 [上架应用](https://developer.fnnas.com/docs/quick-started/publish-application/)。
逐条对照本项目当前状态：

| # | 要求 | HomeEye |
|---|---|---|
| 1 | 材料齐：fnpack 产出的最终 `.fpk` + 图标 + **真实界面截图**（不得占位图/开发期设计图） | ⚠️ 出包后要补截图：多分屏、回放时间轴、设置页三张 |
| 2 | manifest 信息准确，**不得声明超出实测的兼容范围** | ✅ `platform=x86`；`desc` 已按实测改写（不再声称"不拉第三方流媒体服务"，明确小米需自备 SDK） |
| 3 | 安装/打开/运行符合预期；主流程无意外错误 | ⚠️ 1.1.2 需在真机走一遍全新安装（不是升级） |
| 4 | 用户数据可正确保存/读取/更新/删除 | ✅ 设置页改的东西就是落盘的值；录像可删 |
| 5 | 用户无法访问未授权数据/操作 | ✅ 登录闸门 + 认领窗口；未设密码前只放行设密码 |
| 6 | 本地 API/端口/后台任务只暴露真正需要的能力 | ✅ 只监听 unix socket，不开 TCP 端口；go2rtc 那一路另说（见 SECURITY.md） |
| 7 | 权限最小化，默认 `run-as: package` | ✅ 只多一个 `video` 组（核显 `/dev/dri` 需要） |
| 8 | 只声明实际用到的能力；`api-scope` 不要写满 | ✅ 只声明 `data-share`；未用开放 API，所以不写 `api-scope`、不声明 `micro_app` |
| 9 | 不硬编码密钥；`--env` 敏感文件不提交 | ✅ 包里零凭据；`.gitignore` 排除 `data/`、`go2rtc/`、`.env` |
| 10 | 路径/参数不可信、拒绝 `..` 穿越、不暴露配置与私有日志 | ⚠️ 录像目录可指向任意路径是**功能**（用户自己填的），但只在该目录内读写；这一条要在提审材料里说清楚 |

### 社区实测踩坑（**非官方**，只当风险清单）

- `docker-project` 资源的 compose up 早于任何生命周期钩子，离线镜像 `docker load`
  来不及 → `No such image`。本项目因此走 **native 自持进程**，不依赖 Docker。
- 向导 `text` 的 `min/max` 按**字符串长度**校验，端口 `9090` 会被拒 → 用 `pattern`。
  本项目的向导只有一个路径字段，校验全用 `pattern`（见上面「安装时选录像目录」）。
- 桌面 iframe 被 `X-Frame-Options: DENY` 拒 → 需要 CSP `frame-ancestors http: https:`。
  本项目走统一网关 socket，同源转发，不撞这一条。

## 我不确定的东西（提审前值得先问社区主理人）

1. `checksum` 是否被安装端强校验（只验证了 fnpack 的生成算法，没在真机验证篡改后果）。
2. 版本单调性：升级是否必须版本号更高 —— 无官方依据。
3. `os_min_version` 的比较语义（`1.2.0` vs `1.1.3100` vs 本项目的 `1.2.0604`）官方未定义。
   本项目这个值是照 1.0.4 沿用的，**没有逐版本实测**。
4. 商店是否有自动化 lint 或签名要求：文档里没有签名/加密字样，fnpack 也不签名。
5. 具体拒因清单：官方没有成文列表，提交方式是"加飞牛粉丝群 → 联系社区主理人 →
   进应用中心开发者先锋交流群人工提交"，只能靠人工反馈。

## 真机验证清单（发出去之前自己走一遍）

```sh
# 1. 全新安装（不是升级）：先卸干净，再装
#    应用中心 → 手动安装 → 选 homeeye.fpk
# 2. 状态与端口
docker exec ... 不适用；用下面两条看进程与 socket
pgrep -af "homeeye/server/main.py"
ls -l /var/apps/homeeye/target/app.sock
# 3. 图标栏能打开、能进设置页、能改一项并回显同一个值
# 4. 加一台 RTSP 摄像头 → 直播出画面 → 60 秒后 data/recordings 里有分段
# 5. 停止/启动：应用中心点「停止」→ 30 秒内 pgrep 应该没有残留 ffmpeg
# 6. 卸载 → 确认录像还在（本项目刻意保留）
# 7. 升级：装 1.1.2 覆盖旧版 → 配置与录像都在、设置页回显不变
```

## 官方文档索引

[开发者站首页](https://developer.fnnas.com/) ·
[Manifest](https://developer.fnnas.com/docs/core-concepts/manifest/) ·
[应用框架](https://developer.fnnas.com/docs/core-concepts/framework/) ·
[环境变量](https://developer.fnnas.com/docs/core-concepts/environment-variables/) ·
[应用权限](https://developer.fnnas.com/docs/core-concepts/privilege/) ·
[应用资源](https://developer.fnnas.com/docs/core-concepts/resource/) ·
[应用入口](https://developer.fnnas.com/docs/app-entry/) ·
[统一网关](https://developer.fnnas.com/docs/core-concepts/gateway-registration/) ·
[图标](https://developer.fnnas.com/docs/core-concepts/icon/) ·
[fnpack](https://developer.fnnas.com/docs/cli/fnpack/) ·
[创建应用](https://developer.fnnas.com/docs/quick-started/create-application/) ·
[测试应用](https://developer.fnnas.com/docs/quick-started/test-application/) ·
[上架应用](https://developer.fnnas.com/docs/quick-started/publish-application/)
