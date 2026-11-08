# 工具暴露基准

本文记录 2026-11-07 的三批工具面实验：层级、命名与暴露时机。实验回答“同样九个动词，顶层如何组织、如何命名、何时可见”对模型使用的影响。

- 第 1 批只改层级，不改命名。
- 第 2 批只改动词名，层级固定为 H0。
- 第 3 批只改暴露时机，层级与命名固定为 H0。
- 前作 `plan-v3` 做的是删工具与改描述的消融，方向不同，见 [../archive/plan-v3.zh-CN.md](../archive/plan-v3.zh-CN.md)。

## 1. 方案矩阵

| id | 名称 | 顶级工具 | 折叠关系 |
| --- | --- | --- | --- |
| H0 | 平铺基线 | 9 个全部暴露 | — |
| H1 | 观察合并 | observe、call、run、launch、switch、configure、help | find→`observe(query=)`，inspect→`observe(ref=)` |
| H2 | 动作合并 | observe、find、inspect、call、switch、configure、help | run→`call(kind=run,script=)`，launch→`call(kind=launch,command=)` |
| H3 | 看/动二分 | look、do、switch、configure、help | look=observe+find+inspect；do=call+run+launch |
| H4 | 极简二元 | look、do | help→`look(topic=)`；switch→`do(target=)`；configure→`do(configure=)` |
| H5 | 命名空间平铺 | `look.observe`、`look.find`、`look.inspect`、`call.invoke`、`call.run`、`call.launch`、switch、configure、help | 层级写在名字里 |
| H6 | 观察合并+动作合并 | observe、call、switch、configure、help | H1+H2 叠加 |

定义在 `bench/exposure/schemes.py`；装配在 `assemble.py`，合并 schema 由 `TOOL_SPECS` 组合；调度在 `dispatch.py`，折叠调用翻译回平铺 `call_tool`，错误原文透传。

## 2. 评分方法

任务：mock 层 `bench/tasks/tasks-mock-v1.json`，13 条；live 层 `tasks-live-v1.json`，5 条。每条任务由屏幕场景、自然语言指令与运行时状态断言组成。断言按 automation_id 读取终态（文本、值、子项多重集、存在性），不读取 transcript。

指标（`bench/score/metrics.py`）：成功率、工具回合数、语义步数、token、无效调用率、墙钟，以及每个方案的静态注入成本。

token 有两种口径，分别记录：

- 派发会话轮次：从会话可见记录（用户消息、助手可见文本、工具输出）的字符量估算。这是估算口径，不是 API usage，且为下界；隐藏推理与工具参数不计入。
- `bench/loop` 轮次：使用真实 usage（prompt + completion）。

两套口径只在同一通道内比较。

判定（`bench/score/report.py`）：配对单位为（任务，重复），bootstrap 10k、固定种子、95% CI，基线 H0。

- `REJECT`：成功率下降 ≥10pp 且 CI 下界 > 0；
- `ADOPT_CANDIDATE`：成功率非劣，且回合中位下降 ≥15% 或 token 下降 ≥10%，CI 下界 > 0；
- 其余 `PENDING`。

## 3. 运行方式

```bash
# mock 层，单方案
python -m bench.run_exposure --scheme H1 --reps 3 --out runs/exposure

# 全矩阵
python -m bench.run_exposure --scheme all --reps 3 --out runs/exposure

# live 层（子会话；不可用时失败，不回退主桌面）
python -m bench.run_exposure --scheme all --layer live --reps 1 --out runs/exposure-live

# 不接模型的接线冒烟（脚本化假模型）
python -m bench.run_exposure --scheme all --fake --reps 1 --out runs/smoke
```

真模型轮次通过主助手派发独立会话执行。这里的“会话”是 Code 会话，与 live 层的 Windows 子会话不同。一个评分单元（方案 × 任务 × 重复）对应一次派发，续聊使用同一会话。

- 新建：`create_session`，参数为 `prompt`、可选 `title`、必需的 `target`。`target` 为 `{"type":"project","projectId":…,"environment":{"type":"local"}}` 或 `{"type":"projectless"}`。返回 `chat_session_id`、`projectId`、`messageId` 与 `delivery="queued"`。
- 续聊：`send_message_to_session`，传入 `chat_session_id` 与新要求。`delivery="queued"` 表示新开一轮；`"steered"` 表示并入正在运行的一轮。
- 原话：每条派发消息末尾用 `<transcript_evidence>` 块按时间顺序附上用户原话，标注“用户本次 / 用户前一问 / 用户第 N 问”。
- 并行：无依赖的子任务在同一条消息中同时派发；有依赖的按顺序执行，避免多个会话同时写同一批文件或 Git 状态。
- 状态与结果：`list_sessions` 查询持久化状态（`in_progress`、`paused`、`completed`、`canceled`、`failed`）；`read_session` 读取可见历史；`messageId` 用于匹配轮次。派发成功不代表任务完成。

live 层纪律：全部在子会话执行（`LiveEnv.open()`，失败即终止）；每任务独立启动 ScenarioSuite、独立清场；断言使用另一个会话（独立的 peer 连接），断言前释放 agent 连接，下一任务重连。

## 4. 静态注入成本

| 方案 | 工具数 | prompt | schema | 合计 |
| --- | ---: | ---: | ---: | ---: |
| H0 | 9 | 931 | 959 | 1890 |
| H1 | 7 | 896 | 873 | 1769 |
| H2 | 7 | 901 | 800 | 1701 |
| H3 | 5 | 883 | 714 | 1597 |
| H4 | 2 | 840 | 580 | 1420 |
| H5 | 9 | 937 | 959 | 1896 |
| H5u | 9 | 940 | 959 | 1899 |
| H6 | 5 | 862 | 714 | 1576 |

层级折叠的静态收益随折叠程度增加。H4 比 H0 少约 25% 注入 token；H5/H5u 只改名字，合计不低于 H0。

## 5. 第 1 批：层级

配置：`http://127.0.0.1:8317/v1`，模型 `deepseek-v4.1-flash`。端点模型列表中的 `deepseek-v4.1flash` 不可用，实际 id 带连字符。7 方案 × 13 任务 × 3 reps = 273 次；基线 H0 成功率 84.6%（33/39）。原始数据位于 `runs/exposure-mock-final/`。

| 方案 | 成功率差（pp） | 回合差（%） | token 差（%） | 判定 |
| --- | --- | --- | --- | --- |
| H1 | +0.0 [+0.0, +0.0] | −10.8 [−29.1, +4.1] | −0.2 | PENDING |
| H2 | +0.0 [+0.0, +0.0] | −9.9 [−22.2, +1.5] | +1.9 | PENDING |
| H3 | +0.0 [+0.0, +0.0] | −21.8 [−36.2, −7.6] | −3.2 | PENDING |
| H4 | +0.0 [+0.0, +0.0] | −19.5 [−33.2, −7.5] | +6.6 [−4.2, +15.7] | PENDING |
| H5u | −2.6 [−7.7, +0.0] | +0.6 | +1.0 | PENDING |
| H6 | +0.0 [+0.0, +0.0] | −4.8 [−20.3, +8.7] | +9.6 [−2.6, +20.1] | PENDING |

差值为相对 H0；成功率正值表示更好，回合/token 正值表示更省。粗体表示 CI 不含 0。

结果：

1. 成功率与组织方式无关。H0–H6 的成功/失败集合一致，H5u 例外。任务成败由任务本身决定：`mock-chat-two-lines` 的自绘编辑器没有可写接口，`mock-tree-deep-find` 逐层展开超出 12 步上限。
2. 折叠增加回合，两个极端点统计显著。H3 多 21.8% 工具回合，H4 多 19.5%，CI 均不含 0。合并工具后单次回执的定位信息减少，模型用更多回合补充。
3. H6 的 token 中位为 12707，H0 为 15264（−16.8%）；配对差异 +9.6%，CI [−2.6, +20.1] 跨 0；成功率与回合持平。H6 是第 1 批中唯一进入 confirm 的方案。
4. H5 在该端点不可运行：`tools.0.function.name` 带点号被上游拒绝（502，`trae 4027`），标记 `NOT_RUNNABLE(endpoint)`。下划线变体 H5u 可运行，成功率 −2.6pp（CI [−7.7, +0.0]），回合与 token 持平。

第 1 批 screening 裁决：H0 保留；H6 进入 confirm；H3/H4 的极简方向未采纳。

### confirm 批（H0 对 H6，N=65 配对）

| 方案 | 成功率差（pp） | 回合差（%） | token 差（%） | 判定 |
| --- | --- | --- | --- | --- |
| H6 | −1.5 [−4.6, +0.0] | −9.9 [−19.1, −1.4] | +3.7 [−4.4, +10.7] | PENDING |

confirm 未通过。H6 的 token 优势从 +9.6% 降至 +3.7%，CI 跨 0；回合差变为显著增加 +9.9%；成功率 −1.5pp，CI 上界为 0。screening 中的 −16.8% token 中位差为抽样波动。

### 第 1 批最终裁决

H0（平铺九工具）保留，七个层级变体全部未采纳：

- 极简方向（H3/H4）：回合显著增加，CI 不含 0；
- 合并方向（H1/H2/H6）：中性偏好；H6 略省 token，但未通过 confirm；
- 命名方向（H5u）：中性略负；点号形式（H5）存在端点兼容性问题；
- 成功率在所有方案上一致。

### live 层确认

H0 × 5 任务 × 1 rep，全部在子会话执行，断言由独立会话 full 观察后判定。

| 任务 | 结果 | 工具回合 | token |
| --- | --- | ---: | --- |
| live-store-find-add | PASS | 4 | 15013+227 |
| live-form-submit | PASS | 6 | 23320+913 |
| live-menu-new | PASS | 6 | 21098+454 |
| live-dialog-delete | PASS | 6 | 20557+452 |
| live-launch-form | PASS | 3 | 11112+433 |

5/5 PASS，包含 `launch` 在子桌面启动第二个场景并确认窗口出现。两处 live 装置修正：live 行名带价格后缀，断言使用子串匹配；真实 form 应用的校验文本节点不暴露 automation_id，该断言只保留在 mock 层。

## 6. 第 2 批：命名

层级固定 H0，只改动词名。

| 方案 | 改动 |
| --- | --- |
| N1 | 能力名不做别名：`activate`→`invoke`；`somnus/compiler/caps.py` 增加 `NATIVE_CAP_NAMES` 开关，默认关闭 |
| N2 | 工具 `call`→`act` |
| N3 | 工具 `run`→`script` |
| N4 | `observe/find/inspect`→`look/search/peek` |

### screening（13 任务 × 3 reps，基线复用第 1 批 H0）

| 方案 | 成功率差（pp） | 回合差（%） | token 差（%） | 判定 |
| --- | --- | --- | --- | --- |
| N1 不做别名 | +0.0 [+0.0, +0.0] | +10.3 [+0.7, +19.6] | +10.4 [+1.4, +18.9] | ADOPT_CANDIDATE |
| N2 act | −2.6 [−7.7, +0.0] | −7.1 [−23.2, +6.6] | −2.2 | PENDING |
| N3 script | −2.6 [−7.7, +0.0] | +4.2 | +5.2 | PENDING |
| N4 look/search/peek | +0.0 [+0.0, +0.0] | +3.4 | +5.5 | PENDING |

N1 的 screening 结果：`inspect` 调用每任务从 1.31 降至 0.72（−45%），无效调用率从 1.4% 降至 0.5%，回合 +10.3%，token 中位 11836 对 15264（−22%）。

### confirm 批（H0 对 N1，N=65 配对）

| 方案 | 成功率差（pp） | 回合差（%） | token 差（%） | 判定 |
| --- | --- | --- | --- | --- |
| N1 不做别名 | +0.0 [+0.0, +0.0] | −1.3 [−9.3, +6.3] | −1.6 [−8.5, +5.1] | PENDING |

confirm 未通过。screening 的 token −10.4% 与 inspect −45% 在 N=65 上未复现：`inspect` 每任务 1.03（H0）对 0.92（N1），差距为噪声量级；回合、token、成功率持平。

### 第 2 批最终裁决

H0 连同 `activate` 别名保留。四个命名假设全部未采纳：

- 能力名不做别名（`invoke`）：screening 显著，confirm 归零；
- 工具改名（`act` / `script` / `look-search-peek`）：全程中性。

`NATIVE_CAP_NAMES` 保留在 `somnus/compiler/caps.py`，默认关闭。

## 7. 第 3 批：暴露时机

层级与命名固定 H0，只改“何时可见什么”。本批直接运行 confirm 级样本，每方案 13 任务 × 5 reps = 65 配对，基线为第 2 批 confirm 的 H0（130 次运行，0 次基础设施错误）。

| 方案 | 改动 |
| --- | --- |
| T1 | 只暴露 core 六工具，去掉 switch/launch/configure |
| T2 | 最小起步 observe/call/help，调用一次 help 展开全量 |

### 结果（N=65 配对）

| 方案 | 成功率差（pp） | 回合差（%） | token 差（%） | 判定 |
| --- | --- | --- | --- | --- |
| T1 core-only | +0.0 [+0.0, +0.0] | −4.1 [−12.5, +3.6] | +16.1 [+10.2, +21.6] | ADOPT_CANDIDATE |
| T2 help 展开 | +0.0 [+0.0, +0.0] | −17.9 [−31.7, −5.8] | +10.8 [+0.1, +20.7] | 条件性 |

token 中位：H0 14615 → T1 11770（−19.5%）→ T2 12983（−11.2%）。

T1 的收益来自工具 schema 在每次请求中都随 prompt 计费：9 工具 → 6 工具，每步约省 300 token，一个 5 回合任务约省 1500；3 个不可用项不再出现在面里，无效调用率从 0.9% 降至 0.3%。成功率与回合不变。T1 是三批实验中唯一同时满足 confirm 级显著与结构性收益机制的方案。

T2 的净收益较低：每任务多 1 个 help 展开回合（回合差 −17.9%，CI 不含 0），token 节省与 T1 同源。

### 第 3 批裁决

采纳 T1 的原则。现有 `SOMNUS_CAPABILITIES` 分档按该原则收紧：默认 core 档（六工具），`desktop` 档（switch/launch/configure）仅在会话需要子桌面或进程启动时开启。

## 8. 三批总表

| 批 | 方案数 | 采纳 | 未采纳 | 中性保留 |
| --- | --- | --- | --- | --- |
| 1 层级 | 7（含 confirm） | 无 | H3/H4 | H0 |
| 2 命名 | 4（含 confirm） | 无 | — | H0 + `activate` 别名 |
| 3 时机 | 2（confirm 级） | T1 原则（默认收紧到 core 档） | T2 | — |

九工具平铺的 H0 在层级与命名上是本批测量下的最优组织。唯一有量化依据的改进是默认少暴露用不到的工具（T1 / `SOMNUS_CAPABILITIES` core 档），节省约 19% token，行为指标不变。

## 9. 行为等价性验证

`tests/bench/test_exposure.py` 在任何评分开跑前检查：

1. 每个折叠调用与其平铺调用在 `MockProvider` 上产生相同回执；
2. 七个面的 `surface_hash` 互异且装配确定；
3. H0 dispatch 与 `call_tool` 逐工具等价，未知工具错误逐字相同；
4. 七个面都能把同一条 store 任务驱动到相同终态；
5. 假模型端到端（`test_smoke.py`）在 H0 与 H4 上闭环通过。

live 层用假模型冒烟通过：H0 与 H4 都能在真实 ScenarioSuite 窗口上完成 observe→aim→find→add，独立断言会话读到购物车终态。

## 10. 端点排障记录

- 模型列表 id `deepseek-v4.1flash` 不可用，实际 id 为 `deepseek-v4.1-flash`；
- 端点上游拒绝带点号的工具名（`tools.0.function.name`，`trae 4027`），H5 整批 502，标记 `NOT_RUNNABLE(endpoint)`，以 H5u 替代测量；
- 网关偶发 502/503，`LlmClient.chat` 增加三次退避重试。

## 12. ego 借鉴的落地与修复（2026-11-08）

ego 分支两笔提交（EACH/TRY/IF 控制流 + 编译幂等黄金门）评审后的修复与补全：

**修 1 —— EACH 冻结命中集。** 原实现每次 END 重新查询并按索引推进活列表，
体里删除匹配行会在收缩列表上静默跳行（3 行只处理 1、3 行，回执仍
`RUN OK`）。现改为：循环打开时冻结命中集（ref + 指纹 + automation_id）；
每次 END 按指纹在当前树重定位（refs 是位置性的，删行会整体重排，指纹失配
时以**当前树中唯一**的 automation_id 兜底；两路都失即该匹配已消失，跳过并
注记 `~ N match(es) left the tree …`）。循环中新出现的匹配不处理（冻结
语义），需要新集合就重新 FIND。测试：删除逐行/新增行/删除他行三个世界。

**修 2 —— TRY 收窄。** 原 `TRY` 容错一切错误，连 `INVALID_ARGUMENT` 与
`CALL wN.focus()` 的合同性拒绝都被吞掉。现只容许世界状态错误
（TARGET_NOT_FOUND / STALE_TARGET / TARGET_DISABLED / NOT_ACTIONABLE /
TIMEOUT / EFFECT_NOT_CONFIRMED，见 `somnus/errors.py` 的
`TOLERATED_BY_TRY`）；脚本错误照常终止并在回执说明原因。

**落地 3 —— 严格自证 locator 行。** STALE_TARGET 自动重绑本就存在
（registry 结构重定位 → locator 二段策略 → `~ healed old→new`）。新增的
是模型可见输出：`INSPECT` 头节点在定位符**自证通过**（当前树唯一命中且
胜者即本节点，指纹一致）时输出一行 `locator role=… name="…"`；合成名不作为
配方证据；任何疑点整行省略，永不输出 unstable 占位。

**落地 4 —— 指纹位置污染修复（ref 跨轮持有的身份基础）。** 评审中实证：
`ancestor_path` 每层嵌兄弟位置 `#pos`，而 identity 节点并未豁免——删除一行
会让所有后续行的指纹漂移，registry 的 ref 合并随之失效（正是 ego 点 1 担心
的身份冲突）。修复：编译前置一次全窗 automation_id 普查，**全窗唯一**的 id
（`identity_unique`）指纹彻底去位置化；模板重复 id（每行都叫
`item_title`）仍是类而非元素，保留位置区分。修复后删行时幸存行的 ref 由
registry 原样保留（`b1→b2→b3`，不再重排），身份冲突三例（跨父重复 id、
同父重复 id、兄弟删除）各有测试锁定（`tests/runtime/test_fingerprint_identity.py`）。

全套 992 项测试通过；`bench/run_control_flow.py` 数字不变（A 省 1 调用
−29.5% 回执 token，B 无对话框世界省 1 调用 −50%）。

## 11. 分支对比：main 出厂形态 vs play 默认（2026-11-08）

问题：三批实验的结论落到分支上，`play` 相对 `main` 的黑盒表现差多少。

装置：`git worktree` 检出 main（`6d21b42`），**main 原样出厂臂** = main 自己的运行时 + 它的 8 工具 `TOOL_SPECS`（当时还没有 help 工具与分档系统）+ 它自己的 system_prompt 原文；任务集、fixture、被测模型（deepseek-v4.1-flash）与 play 各臂完全一致。65 次（13 任务 × 5 reps），0 次基础设施错误。驱动脚本 `scratch/run_main_branch.py`，原始数据 `runs/exposure-main-branch/` 与 `runs/exposure-branch-compare/`。

| 臂 | 工具数 | 成功率 | 回合中位 | token 中位 |
| --- | --- | --- | --- | --- |
| MAIN（main 出厂） | 8 | 84.6% | 4.0 | 21734 |
| H0（play，9 工具 + 短核心提示） | 9 | 84.6% | 4.0 | 14615 |
| T1（play 默认，core 档） | 6 | 84.6% | 4.0 | 11770 |

配对对比（bootstrap 10k，95% CI，基线 = MAIN）：

| 对比 | 成功率差 | 回合差 | token 差 |
| --- | --- | --- | --- |
| H0（play）vs MAIN | +0.0 | +13.5% 更省，CI 不含 0 | +32.7% 更省，CI 不含 0 |
| T1（play 默认）vs MAIN | +0.0 [+0.0, +0.0] | +1.5% [−6.6, +9.5]，持平 | **+39.4% 更省 [+34.4, +44.1]** |

三条读数：

1. 成功率纹丝不动。三个臂的成功/失败集合完全一致（同为 chat-two-lines 与 tree-deep-find）。从 main 到 play 的全部改动——分层探测、短核心提示、T1 默认档——没有让模型多做成任何一条任务，也没有做坏任何一条。
2. token 成本接近腰斩。play 默认相对 main 出厂每任务省 39.4% token（CI [+34.4, +44.1]），回合持平。来源两部分：play 的短核心提示与运行时优化（约 −33%），T1 默认收紧再叠加（约 −6pp）。
3. 对照实验与出厂对比相互印证。受控 A/B（T1 对 H0，同分支）测得 −16.1%；出厂对比里 T1 的增量贡献约 −6pp，差值来自 MAIN 臂本身没有短提示，两段证据在各自口径内自洽。

分支裁决：play 相对 main 是纯成本优化加行为持平，黑盒数据支持合入。
