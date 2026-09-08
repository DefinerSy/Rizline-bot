# 官方 QQ Python 机器人

本项目代码采用 [MIT License](LICENSE) 开源。第三方项目、RizLine 游戏资源、曲绘、字体、头像、背景和玩家存档不随本仓库分发，相关权利与许可证以各自项目及权利人的说明为准。

这是一个基于腾讯官方 `qq-botpy` SDK 的最小常驻机器人：

- 群聊中被 `@` 时回复；
- 收到已授权的 C2C（私聊）消息时回复；
- 通过 Gateway 长连接运行，不需要为机器人配置公网 HTTPS 回调地址；
- 凭据只保存在本机 `.env`，不会进入 Git。

## 引用项目与致谢

感谢以下社区项目为本项目提供参考与配套工具：

- [CHCAT1320/RizlineGameSaveData](https://github.com/CHCAT1320/RizlineGameSaveData)：游戏登录、存档拉取及本地存档解密相关工具与接口参考。
- [CHCAT1320/rizline-assets-get](https://github.com/CHCAT1320/rizline-assets-get)：游戏资源导出工具；本项目兼容其已导出的曲绘、头像、背景等本地素材。
- [REDDRAGON-HL/rizline_b40_tool](https://github.com/REDDRAGON-HL/rizline_b40_tool)：B40 展示与 AH5+B35 成绩选择行为参考。

各项目的使用方式及许可证边界详见下文[参考项目与边界](#参考项目与边界)。本仓库不分发游戏资源、玩家存档或上游工具的登录配置。

## 1. 在 QQ 开放平台完成配置

在机器人对应应用的控制台中取得 **AppID** 和 **AppSecret**。机器人显示的 ID 本身不足以让程序登录。

同时确认已开通并订阅以下消息场景：

1. `群聊 @ 机器人消息`；
2. `C2C 私聊消息`；
3. 测试阶段所需的测试用户、测试群或沙箱配置。

官方群机器人受开放平台的场景和权限限制：它不会监听普通 QQ 号的所有会话。群聊通常要用户 `@机器人` 才会触发；私聊也必须是开放平台已授权的 C2C 场景。

## 2. 本机启动

建议使用当前腾讯 SDK 支持的稳定 Python 版本（通常 Python 3.10–3.12）。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

如果 `venv` 报 `ensurepip is not available`，说明系统缺少对应 Python 的 venv 组件；请安装匹配版本的系统包后重试，或使用下面的 Docker 方式。Debian/Ubuntu 上通常是 `python3-venv` 或类似 `python3.12-venv` 的包。

编辑 `.env`，填入真实凭据：

```env
QQ_APP_ID=你的AppID
QQ_APP_SECRET=你的AppSecret
# 如果控制台明确要求使用沙箱，改为 true；正式环境保持 false
QQ_IS_SANDBOX=false
# auto 只会在本机把 *.qq.com 解析到私网地址时启用公开 DNS 回退
QQ_DNS_FALLBACK=auto
```

启动：

```bash
python bot.py
```

在测试群 `@机器人 /ping`，预期会收到 `pong`；在已开启 C2C 权限的私聊中发送 `/ping` 也应得到同样结果。程序会自动忽略群消息开头的机器人提及标记。日志出现 `connected and ready` 表示 Gateway 已连接。

本项目已在当前机器的 Python 3.14 和 `qq-botpy 1.2.1` 下完成依赖与客户端初始化验证。代码会为该版本 Python 显式创建 BotPy 所需的 asyncio 事件循环；若你在其他服务器部署，Python 3.10–3.12 仍是较稳妥的选择。

若本机网络把 QQ API 域名错误解析成私网 IP，`QQ_DNS_FALLBACK=auto` 会仅为 `*.qq.com` 使用公开 DNS-over-HTTPS 查询，并保留 TLS 的原始域名校验；它不会修改系统 DNS 或 hosts。正常网络可将其设为 `off`。

## 3. 修改回复逻辑

机器人消息入口在 [`bot.py`](bot.py) 的两个方法：

- `on_group_at_message_create`：群内 `@` 消息；
- `on_c2c_message_create`：私聊消息。

实际回复规则集中在 `ReplyService.reply_for()`。普通消息不再原样回显，以免会话过期后的手机号或验证码被重复发送。登录网络请求使用异步调用，并设置超时和错误处理。

## RizLine（律动轨迹）查分

机器人支持读取管理员导入的本地存档，也支持玩家在 QQ 私聊中自行短信登录、拉档并绑定。自助登录不接受密码或用户粘贴的登录令牌，不把手机号、验证码、游戏令牌或完整存档写入日志。QQ 平台仍可能保留玩家发送的聊天记录。

将存档放在 `data/rizline_saves/`，文件名就是玩家别名。例如：

```text
data/rizline_saves/default.json
data/rizline_saves/alice.json
```

在服务器上从一个你已自行导出的 JSON 文件复制即可；不要把完整存档发进群聊或私聊，因为它可能包含游戏内邮件、货币、购买记录和用户标识。示例：

```bash
mkdir -p data/rizline_saves
cp /安全位置/已解密存档.json data/rizline_saves/default.json
chmod 600 data/rizline_saves/default.json
```

也可在 `.env` 中指定目录与默认别名：

```env
RIZLINE_SAVE_DIR=data/rizline_saves
RIZLINE_DEFAULT_PLAYER=default
```

### 玩家自助绑定：全程 QQ 私聊

无需管理员发码，也无需公网网站。玩家在已授权的 C2C 私聊中依次操作：

1. 发送 `/riz login`，阅读说明后回复 `同意并保存`；若不希望保留登录令牌，回复 `仅本次登录` 或 `同意`。
2. 发送自己账号的 11 位手机号；机器人请求一条游戏登录短信。
3. 在 2 分钟内只发送数字验证码，不要转发整段短信。
4. 核对返回的游戏昵称与谱面数量，回复 `确认` 或 `/riz confirm`。
5. 发送 `/riz b40` 查询。选择加密保存的玩家以后私聊 `/riz update`，或在群里 `@机器人 /riz update`，即可更新自己的成绩，无需每次请求短信。

`/riz resend` 重发验证码，`/riz cancel` 随时取消，`/riz unbind` 解除绑定。群消息不能发起、继续、确认或取消私聊登录。只在玩家明确同意且提供手机号后请求短信，不会在服务启动或查询 B40 时自动发短信。

群更新只使用 QQ 事件中的发送者身份匹配已有绑定，不接受玩家别名或登录参数，不会猜测其他账号。群聊与私聊共用每玩家的更新冷却及在途会话。未匹配到绑定、未保存令牌或令牌失效时，引导玩家回到私聊登录，不在群里收取手机号或验证码；群内更新期间私聊解绑或换绑，晚到结果也不能恢复旧绑定。

自助功能默认随绑定服务启用，可设置 `RIZLINE_QQ_LOGIN_ENABLED=false` 关闭；需要本地 `vendor/RizlineGameSaveData/gameDataAes2Json.py` 和 requirements 中的 aiohttp、pycryptodome。`RIZLINE_LOGIN_CHANNEL_ID` 默认 1，可由管理员设置为 1～11。当前以社区工具的手机号账号流程为范围，不支持国际服邮箱、官方扫码或绕过游戏端安全验证。

每个 QQ 使用独立内存会话和设备 ID，会话绝对期限 10 分钟，最多 32 个会话、4 个同时执行的网络操作；单次操作最长 30 秒。短信至少间隔 60 秒，每手机号每小时 3 次、每 QQ 每小时 5 次、全局每小时 20 次；失败请求也计入，验证码最多尝试 5 次。限流只保留带随机密钥的身份摘要和时间戳，重启会重置计数并取消全部会话。

只有玩家选择“同意并保存”并最终确认账号后，才用 AES-256-GCM 加密保存 token、手机号、设备 ID、渠道和游戏账号 ID。验证码始终不落盘。已有玩家必须重新登录一次授权，之前被丢弃的令牌无法补回；不会使用管理员工具里的令牌代替玩家授权。令牌不是永久有效：读取到的 JWT 到期时间或本机最多七天的使用期限到达后需重新登录，游戏端也可能提前撤销。正常刷新不会自行延长同一令牌的有效期；游戏端返回新令牌时才更新。网络错误、限频及存档校验失败保留原凭据和成绩；明确 401 鉴权失败会清除该版本凭据并要求重新登录。

凭据数据库默认在 `data/rizline_tokens.sqlite3`，自动生成的独立 32 字节主密钥在 `.secrets/rizline-token.key`；两者均只允许当前系统用户读写（600），新建密钥目录权限为 700。可通过 `RIZLINE_TOKEN_DB`、`RIZLINE_TOKEN_KEY_FILE` 修改路径，不能放在成绩、图片导出或资源目录，不能上传到 COS、放入版本控制或发给别人。备份时将密钥和数据库分开保护；重启会继续使用原密钥，密钥丢失或不匹配时拒绝使用数据库，绝不自动轮换或回退明文。加密不防能控制本机及读取密钥的管理员；QQ 端的登录聊天记录也无法由机器人删除。过期记录在读取时删除，不能把七天使用期限理解为备份或磁盘密文会按时自动销毁。

每 QQ 的更新至少间隔 60 秒，全局每分钟最多 20 次，且共用最多 4 个网络操作的并发限制，不占用短信配额。成功确认或更新后，以 600 权限保存白名单成绩及名片展示字段；成绩 JSON 不含手机号、验证码、token、游戏内邮件或购买记录。更新生成新的随机别名快照，不覆盖旧文件；绑定版本核对可阻止解绑或换绑后的晚到结果写回。新凭据先加密暂存再发布绑定，发布失败清理本次数据，保留旧成绩与旧凭据；跨文件并非单一数据库事务，进程被强制终止可能留下不可用于当前绑定的孤立密文，下一次成功登录或解绑会清理。`/riz unbind` 删除该 QQ 的本机凭据并解除映射，但不删除已有成绩文件；手动绑定码换绑也撤销原令牌。解绑或手动换绑若在撤销后发生磁盘故障，会安全地停止，但原令牌可能已经删除。

自助流程不执行会打印凭据的上游登录脚本，也不接触其共享配置和存档。它只访问固定游戏 HTTPS 登录/拉档接口，开启证书验证、禁用重定向、限制响应体大小；BotPy 原始 DEBUG 消息和错误响应内容会被过滤。此功能是社区查分适配，不是鸽游官方授权服务，游戏接口变更或风控可能导致登录失败。

短信接口与账号登录接口分开校验：短信 HTTP 2xx 回包若为空或格式暂无法识别，不宣称已发送，但会保留验证码输入步骤。玩家已收到短信可直接输入验证码，无需重复请求；没收到可在冷却后 `/riz resend`。明确业务错误、非 2xx 和 429 仍拒绝；真正登录仍要求成功业务码、有效返回令牌及通过认证解密的存档。异常日志仅记录步骤、状态码或格式分类，不记录短信回包正文。

### 管理员本机登录、拉档与导入

以下是保留的管理员导入方式，与玩家自助私聊登录相互独立。若你自行决定使用 [CHCAT1320/RizlineGameSaveData](https://github.com/CHCAT1320/RizlineGameSaveData) 拉取自己的存档，可使用本项目的**本机回环地址**管理页：它只监听 `127.0.0.1`，不能映射到公网、反向代理或 QQ 回调。

先在本机准备该独立工具及其隔离虚拟环境：

```bash
mkdir -p vendor
git clone --depth 1 https://github.com/CHCAT1320/RizlineGameSaveData.git vendor/RizlineGameSaveData
python3 -m venv vendor/RizlineGameSaveData/.venv
vendor/RizlineGameSaveData/.venv/bin/pip install requests urllib3 pycryptodome
```

启动本机页面：

```bash
.venv/bin/python tools/local_rizline_login.py
```

在**这台机器的浏览器**打开 `http://127.0.0.1:8765/`。页面会在本机将信息交给独立拉档工具，丢弃其敏感终端输出，导入 `gameData.json` 并生成一次性 QQ 绑定码；不会把登录信息交给机器人、日志或 COS。本项目的启动包装器会强制开启 HTTPS 证书校验，即使上游脚本请求关闭校验。若通过 SSH 远程管理机器，请使用 SSH 本地端口转发访问，不要开放 8765 端口。引用项目为 GPL-3.0，本项目不复制其登录代码，只作为独立本机进程调用。

也可不使用页面，直接在管理员终端运行该项目后，用导入工具将 `gameData.json` 复制到机器人的受限目录：

```bash
.venv/bin/python tools/import_rizline_save.py /安全位置/gameData.json default
```

同一别名已有存档时，导入工具默认拒绝覆盖；确认要更新时再明确加上 `--replace`。导入后的文件权限为 `600`。

### QQ 用户与本地存档绑定

可将一个 QQ 用户绑定到一份**已经导入**的本地存档，无需也不会传递游戏登录信息。管理员为对应别名创建一次性绑定码：

```bash
.venv/bin/python tools/create_rizline_binding_code.py default
```

绑定码默认 15 分钟有效、只可使用一次，数据库只保存绑定码的哈希和 QQ OpenID 到玩家别名的映射，文件权限为 `600`。请将终端显示的码私下发给对应用户；用户在与机器人的 **C2C 私聊**中发送：

```text
/riz bind <绑定码>
```

绑定数据库启用时，`/riz profile`、`/riz top`、`/riz b40` 和 `/riz song <关键词>` 都需要先绑定，随后默认查询自己的存档，且不能通过附带其他别名读取别人的已绑定存档。用户可在 C2C 私聊中发送 `/riz binding` 查看状态，或 `/riz unbind` 解除绑定。群内不接受绑定码，避免意外公开。

默认绑定数据库路径为 `data/rizline_bindings.json`，可用 `RIZLINE_BINDING_DB` 调整；设为空才会关闭绑定要求、恢复旧的共享默认玩家行为。不要把绑定码、完整存档或游戏登录信息发送到群聊。

存档文件不进入版本控制。机器人只读取 `username`、`totalRks`、`myBest` 和 `levelsRks`，可用命令：

```text
/riz help
/riz profile [玩家别名]
/riz top [玩家别名] [1-20]
/riz b40 [玩家别名]
/riz song <关键词>
/riz <玩家别名> song <关键词>
```

项目已附带不含真实资料的 `demo.json`，可先在 QQ 中测试：`/riz demo profile`、`/riz demo top 3`、`/riz demo b40` 和 `/riz demo song Sky`。

### B40 规则、图片导出与曲目目录

新版采用游戏风格的浅青白底、点阵与圆环装饰、大圆角成绩卡。完整 AH5+B35 图片为 1650×2082，定数缺失时为 1650×2012 的 TOP 40；后者完整显示 40 个位置。曲名、难度与定数、单曲 RKS、分数和完成率分别排版，空位淡化显示。顶部主值是本次计算的 B40 RATING，存档 TOTAL RKS 单独标注。

若本地资源齐全，会使用导出的游戏混合字体、浅色卡片纹理、玩家选中的背景与名片布局、头像和中文称号；缺少玩家头像时先尝试游戏默认头像，再回退为首字母。字体缺失或损坏会使用系统字体。玩家头像保持等比例完整缩放，不按存档坐标放大裁切。

`/riz b40` 会生成一张 1650px 宽的 PNG 成绩卡：顶部为玩家和 B40 值，接着是 AH5 与 B35 五列卡片。未配置曲绘时使用通用难度图标。

如果你拥有使用游戏资源的授权，可选用 [CHCAT1320/rizline-assets-get](https://github.com/CHCAT1320/rizline-assets-get) 在**本机**导出曲绘。该仓库没有声明可供本项目复用的代码许可证，且其 `main.py` 会交互式清空自身的 `download/` 目录并进行大规模、覆盖式下载；因此机器人不会自动运行它。请在隔离、可丢弃的目录中自行审查和运行，并遵守游戏资源的使用条款。

导出完成后，将它的 `output/` 目录配置为只读曲绘缓存，再重启机器人：

```env
RIZLINE_ARTWORK_DIR=/安全位置/rizline-assets-get/output
```

机器人只读本地 `output/default.json`、曲绘目录以及 `layouts/`、`avatars/`、`localization/` 和指定的 `ui/Base/` 字体/纹理；优先使用 HiRes 曲绘并裁成圆形封面。不会在 QQ 查询期间联网下载、更新或暴露原始资源文件，生成的 B40 图仍通过当前配置的私有 COS 短时签名链接发送。

计算需要本地曲目定数、Hit 和 RizHit 资料。先手动更新一次缓存；机器人**不会**在用户查询时联网：

```bash
.venv/bin/python tools/update_rizline_catalog.py
```

该命令从 `rizline_b40_tool` 可访问的 RizLine 中文维基缓存下载资料，并写入 `data/rizline_song_catalog.json`。刷新失败时，已有目录不会被覆盖；目录和导出的图片都已被 `.gitignore` 排除。需要更新时再运行一次即可。

目录齐全时，计算遵循参考工具的 AH5+B35 行为：优先选择可确认 AH，随后用标为 `?AH` 的兼容候选补足，再选 B35；总和始终除以 40。若存档中的任一谱面不在本地目录中，机器人会明确退回 `RKS TOP 40（定数数据不完整）`，不会把近似结果伪装成 AH5+B35。

默认情况下 PNG 写到 `data/rizline_exports/`。QQ 官方富媒体接口要求机器人提供一个可由 QQ 服务器访问的图片 URL；它不能直接读取这台机器的本地文件。

推荐方式是不部署网站，而是使用**私有腾讯 COS 存储桶**：机器人上传图片后生成 10 分钟有效的 HTTPS 签名链接，QQ 立即拉取并发送。图片不会开放匿名访问或被公开列目录。创建私有桶后，新建只允许对应存储桶和前缀 `PutObject`、`GetObject` 的 CAM 子账号/临时凭据，将下列值填入本机 `.env`：

```env
RIZLINE_IMAGE_OUTPUT_DIR=data/rizline_exports
RIZLINE_COS_BUCKET=your-bucket-1250000000
RIZLINE_COS_REGION=ap-guangzhou
RIZLINE_COS_SECRET_ID=你的COS SecretId
RIZLINE_COS_SECRET_KEY=你的COS SecretKey
RIZLINE_COS_PREFIX=qq-rizline-b40
RIZLINE_COS_URL_EXPIRES_SECONDS=600
```

不要把 COS 密钥发送到 QQ、聊天窗口或提交到 Git。COS 方式与静态站点方式二选一；填好后重启机器人，在群内 `@机器人 /riz b40 [玩家别名]` 或私聊发送同样命令，即会先收到文字结果，再收到图片。建议在 COS 生命周期规则中为该前缀设置自动删除（例如 1 天），避免生成图片长期留存。

机器人默认读取 `.env`；也兼容将 COS 字段单独放进受限权限的 `env.env`，便于与 QQ 凭据分开保存。

最小 CAM 权限示例（将地域、账号 ID、桶名和前缀替换为你的实际值；不要授予全桶或全部 COS 权限）：

```json
{
  "version": "2.0",
  "statement": [{
    "effect": "allow",
    "action": ["name/cos:PutObject", "name/cos:GetObject"],
    "resource": ["qcs::cos:ap-guangzhou:uid/1250000000:your-bucket-1250000000/qq-rizline-b40/*"]
  }]
}
```

如果暂时不使用 COS，仍可把**仅此导出目录**映射到 HTTPS 静态站点，并配置相同 URL 前缀：

```env
RIZLINE_SONG_CATALOG_PATH=data/rizline_song_catalog.json
RIZLINE_IMAGE_OUTPUT_DIR=data/rizline_exports
RIZLINE_IMAGE_PUBLIC_BASE_URL=https://bot.example.com/rizline-b40/
```

例如 Nginx 可将公开路径精确映射到导出目录（替换为部署机器上的绝对路径，并启用 HTTPS 证书）：

```nginx
location /rizline-b40/ {
    alias /srv/qq-official-bot/data/rizline_exports/;
    autoindex off;
    add_header Cache-Control "private, no-store";
}
```

不要把 `data/` 或 `data/rizline_saves/` 整体公开。若两种图片发送配置都留空，机器人仍会生成本地 PNG，但会在 QQ 中说明图片无法发送；这避免将成绩图自动上传到未知第三方。

本机自检：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

### 参考项目与边界

- [CHCAT1320/RizlineGameSaveData](https://github.com/CHCAT1320/RizlineGameSaveData) 是 GPL-3.0 的社区存档工具。本机管理页通过独立进程调用它；QQ 自助短信适配独立实现网络与会话处理，并从本地解密模块加载解密参数及字节转换函数，不执行其登录入口。使用、修改或分发引用组件时须遵守其许可证；该流程不代表游戏方提供了开放授权接口。
- [CHCAT1320/rizline-assets-get](https://github.com/CHCAT1320/rizline-assets-get) 可导出本地游戏资源，但仓库未声明代码许可证且资源本身的权利仍归原权利人；本项目只兼容其已导出的本地图片目录，不执行或分发其代码和资源。
- [REDDRAGON-HL/rizline_b40_tool](https://github.com/REDDRAGON-HL/rizline_b40_tool) 是 Apache-2.0 的 B40 前端参考。本项目独立实现其 AH5+B35 选择行为，详情见 [`NOTICE`](NOTICE)；其私有后端不作为运行时依赖。目录更新只在管理员主动执行时读取可用缓存，查询路径始终只读本地资料。

## 4. 作为 Linux 服务常驻运行

### 同步 QQ 菜单与指令面板

`tools/sync_qq_commands.py` 使用官方菜单/面板 API，同步私聊底部的 B40、查分、账号、登录操作四个入口，以及私聊 13 条、群聊 7 条指令。群聊面板包含 `/riz update`，但不提供登录、验证码或解绑操作。菜单项填入对应命令；单曲查询和手动绑定码还需玩家补充参数。

先在项目目录只读检查，确认目标 AppID 是当前机器人：

```bash
.venv/bin/python tools/sync_qq_commands.py --expect-app-id 你的AppID
```

明确提交时再加 `--apply`。工具使用已有本机配置鉴权，不输出令牌；修改前将菜单及面板配置以 600 权限备份到 `data/qq_menu_backups/`。保留无关菜单，遇到名称冲突或未知的已有全局面板时停下，不盲目覆盖。已同步的内容再次执行会跳过，不重复创建面板；更新后回读确认。若网络中断，先不带 `--apply` 重新检查已生效的内容，不直接反复创建。

QQ 面板接口会将指令名称开头的 `/` 去掉，并省略值为 false 的默认字段；同步工具按其返回格式核对，机器人同时支持 `/riz ...` 和 `riz ...`。菜单变更不需要重启机器人。公开接口说明见 QQ 官方文档的“自定义菜单与指令面板”章节。

编辑 [`systemd/qq-official-bot.service`](systemd/qq-official-bot.service)，将 `User`、`WorkingDirectory`、`EnvironmentFile` 和 `ExecStart` 改为本机实际路径。然后以有系统服务管理权限的用户执行：

```bash
sudo cp systemd/qq-official-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qq-official-bot
sudo systemctl status qq-official-bot
```

查看实时日志：

```bash
journalctl -u qq-official-bot -f
```

如果这只是临时的在线开发环境，进程会随环境回收而停止；正式使用请部署到一台能长期运行、可出网的 Linux 主机或容器平台。

## 5. Docker 方式（推荐用于这类服务器环境）

项目附带固定 Python 3.12 的 `Dockerfile` 和 `compose.yaml`，避免依赖宿主机的 Python 版本。创建并填写 `.env` 后运行：

```bash
docker compose up -d --build
docker compose logs -f
```

公开镜像不包含 `vendor/` 或游戏存档。若要在 Docker 中启用短信登录，需在运行时只读挂载经过审查的解密模块 `gameDataAes2Json.py` 到 `/app/vendor/RizlineGameSaveData/gameDataAes2Json.py`，不要挂载上游工具的配置和原始存档；缺少模块时自助登录关闭，已有存档查询仍可用。Compose 分开持久化 `data/` 与 `.secrets/`，容器重建时必须保留原密钥。不要把环境文件、密钥或运行数据放入镜像，也不要提交可能展开密钥的 `docker compose config` 输出。

停止服务：

```bash
docker compose down
```
