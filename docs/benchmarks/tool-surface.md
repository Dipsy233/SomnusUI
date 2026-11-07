# 工具暴露基准（第 1–3 批：层级、命名、时机）

> 回答一个问题:**同样的九个动词，顶层怎么组织、怎么命名、什么时候暴露，LLM 用得最好?**
> 第 1 批只动层级(谁暴露为顶级工具、谁折叠成参数),不改任何命名——
> `observe/find/inspect/call/run/launch` 在每个方案里含义相同。
> 第 2 批只改动词名，第 3 批只改暴露时机；结果分别见下文对应章节。
> 前作 plan-v3（现归档于 [plan-v3.zh-CN.md](../archive/plan-v3.zh-CN.md)）做的是
> 删工具/改描述的消融，方向不同；本设施在 `bench/` 下独立新建。

## 方案矩阵

| id | 名称 | 顶级工具 | 折叠关系 | 设计理由 |
|---|---|---|---|---|
| **H0** | 平铺基线(现状) | 9 个全暴露 | — | 对照原点 |
| **H1** | 观察合并 | observe、call、run、launch、switch、configure、help | find→`observe(query=)`,inspect→`observe(ref=)` | 观察类意图同源,少两个 schema |
| **H2** | 动作合并 | observe、find、inspect、call、switch、configure、help | run→`call(kind=run,script=)`,launch→`call(kind=launch,command=)` | 动作类一个入口 |
| **H3** | 看/动二分 | look、do、switch、configure、help | look=observe+find+inspect;do=call+run+launch | 每类意图一个工具,顶级最少且对称 |
| **H4** | 极简二元 | look、do | help→`look(topic=)`;switch→`do(target=)`;configure→`do(configure=)` | 极端点,测最少工具面的下限 |
| **H5** | 命名空间平铺 | `look.observe/look.find/look.inspect/call.invoke/call.run/call.launch`+switch/configure/help | 不折叠 dispatch,层级只写在名字里 | 对照"层级在名字里"与"折叠进参数" |
| **H6** | 观察合并+动作合并 | observe、call、switch、configure、help | H1+H2 叠加,保留 help/configure/switch | 验证两个方向的收益是否可加 |

定义在 `bench/exposure/schemes.py`(纯数据);装配在 `assemble.py`(合并
schema 一律由 `TOOL_SPECS` 组合,不手抄;system prompt 只重生成 Tools 表,
正文不动,折叠动词的映射以一行注记补在表下);调度在 `dispatch.py`(折叠
调用翻译回平铺 `call_tool`,错误原文透传——折叠面不会比基线更宽容)。

## 评分

**任务**(mock 层 `bench/tasks/tasks-mock-v1.json`,13 条;live 层
`tasks-live-v1.json`,5 条):每条 = 屏幕场景 + 自然语言指令 + **运行时状态
断言**(按 automation_id 读终态:文本/值/子项多重集/存在性),不看
transcript、不按"看起来做完了"判分。

**指标**(`bench/score/metrics.py`):成功率、工具回合数、语义步数、token、
无效调用率、墙钟;另计每个方案的**静态注入成本**(system prompt + 全部工具
schema 的 token)。

token 有两套口径,分开记、不合并成一个数字:

- **派发会话轮次**:拿不到 usage——Trae 本地主库加密,日志里没有数值型用量,
  会话工具返回值也不带 usage,`messageId` 只能对轮次。取数来源是**会话可见
  记录**(用户消息、助手可见文本、工具输出)的**字符量**。这是估算口径,不是
  API usage,且是**下界**:隐藏推理与工具参数不在可见记录里,不计入。
- **`bench/loop` 轮次**:本来就有真实 usage(prompt+completion),沿用原口径。

两套口径各自只在同一通道内比较,不跨通道合并。

**判定**(`bench/score/report.py`):配对单位 = (任务, 重复),bootstrap
10k / 固定种子,95% CI,基线 H0。刻意轻量(v3 的预注册协议不在本批范围):

- `REJECT`:成功率降 ≥10pp 且 CI 下界 > 0;
- `ADOPT_CANDIDATE`:成功率非劣,且回合中位降 ≥15% 或 token 降 ≥10%
  (CI 下界 > 0;token 按上节同一口径,在同一通道内比较——派发会话用可见记录
  的字符量,`bench/loop` 用 API usage);
- 其余 `PENDING`。

## 跑法

```bash
# mock 层,单方案
python -m bench.run_exposure --scheme H1 --reps 3 --out runs/exposure

# 全矩阵
python -m bench.run_exposure --scheme all --reps 3 --out runs/exposure

# live 层(子会话;不可用时大声失败,绝不回退主桌面)
python -m bench.run_exposure --scheme all --layer live --reps 1 --out runs/exposure-live

# 不接模型的接线冒烟(脚本化假模型,CI 可跑)
python -m bench.run_exposure --scheme all --fake --reps 1 --out runs/smoke
```

真模型轮次不调用模型 API,而是由主助手**派发一个独立会话**(dispatch a
session):执行体是本地 Code 会话,创建即入队、异步执行。这里的"会话"指 Code
会话,与下文 live 层的 Windows 子会话不是一回事。一个评分单元(方案 × 任务 ×
重复)对应一次派发,续聊用同一个会话;会话之间没有父子 run id、没有恢复钩子,
子会话也不继承主助手的完整上下文。

- 新建:`create_session`,参数为 `prompt`、可选 `title`、必需的 `target`;
  `target` 是 `{"type":"project","projectId":…,"environment":{"type":"local"}}`
  或 `{"type":"projectless"}`。返回 `chat_session_id`、`projectId`、
  `messageId` 与 `delivery="queued"`。`queued` 只表示消息被接收,不代表开始,
  也不代表完成。
- 续聊:`send_message_to_session`,传 `chat_session_id` 与新的要求。
  `delivery="queued"` 表示新开一轮;`"steered"` 表示并入正在运行的那一轮、
  不打断当前工作。它不构成对等待中工具调用的批准。
- 原话:每条派发消息末尾用 `<transcript_evidence>` 块按时间顺序附上用户原话,
  标注 `用户本次:` / `用户前一问:` / `用户第 N 问:`,原话不改写。
- 并行:互不依赖的子任务在同一条消息里用多个 `create_session` /
  `send_message_to_session` 同时派发;有依赖关系的必须顺序执行,并避免多个
  会话同时写同一批文件或 Git 状态。
- 状态与结果:`list_sessions` 查持久化状态(`in_progress` / `paused` /
  `completed` / `canceled` / `failed`,只是快照,不是实时保证);`read_session`
  读可见历史;用 `messageId` 匹配轮次来确认某条派发消息的进展。子会话的终止
  回复由系统投递回主助手。不轮询等待,不伪造结果:派发成功不等于任务完成。

上面几条命令负责装配与 mock / 假模型轮次;真模型的每个评分单位由一次派发
产生,不经过 `LlmClient`。对比时固定同一个模型——模型是测量仪,不是变量。

**live 层纪律**(沿用 plan-v3 的硬边界):全部在子会话执行
(`LiveEnv.open()` 开,失败即终止);每任务独立启动 ScenarioSuite、独立
清场(只杀任务期间新出现的 pid);断言用**另一个会话**(自己的 peer 连接,
peer 一次只服务一个客户端,因此断言前先释放 agent 连接、下一任务重连)。

## 静态注入成本(已测,与被测模型无关)

| 方案 | 工具数 | prompt | schema | 合计 |
|---|---|---|---|---|
| H0 | 9 | 931 | 959 | 1890 |
| H1 | 7 | 896 | 873 | 1769 |
| H2 | 7 | 901 | 800 | 1701 |
| H3 | 5 | 883 | 714 | 1597 |
| H4 | 2 | 840 | 580 | 1420 |
| H5 | 9 | 937 | 959 | 1896 |
| H5u | 9 | 940 | 959 | 1899 |
| H6 | 5 | 862 | 714 | 1576 |

层级折叠的静态收益单调:H4 比 H0 少 ~25% 注入 token;H5/H5u(只改名字)反而
比 H0 略贵——层级写在名字里没有 schema 收益。

## 第 1 批评分结果(deepseek-v4.1-flash,mock 层,2026-11-07)

配置:`http://127.0.0.1:8317/v1` / `deepseek-v4.1-flash`(注意:该端点模型
列表里的 `deepseek-v4.1flash` 不可用,实际 id 带连字符);7 方案 × 13 任务 ×
3 reps = 273 次;基线 H0 成功率 **84.6%**(33/39,非天花板,有区分头寸)。
原始数据 `runs/exposure-mock-final/`,判定表:

| 方案 | 成功率差(pp) | 回合差(%) | token 差(%) | 判定 |
|---|---|---|---|---|
| H1 | +0.0 [+0.0, +0.0] | −10.8 [−29.1, +4.1] | −0.2 | PENDING |
| H2 | +0.0 [+0.0, +0.0] | −9.9 [−22.2, +1.5] | +1.9 | PENDING |
| H3 | +0.0 [+0.0, +0.0] | **−21.8 [−36.2, −7.6]** | −3.2 | PENDING |
| H4 | +0.0 [+0.0, +0.0] | **−19.5 [−33.2, −7.5]** | +6.6 [−4.2, +15.7] | PENDING |
| H5u | −2.6 [−7.7, +0.0] | +0.6 | +1.0 | PENDING |
| H6 | +0.0 [+0.0, +0.0] | −4.8 [−20.3, +8.7] | +9.6 [−2.6, +20.1] | PENDING |

(差值为相对 H0:成功率正=更好;回合/token 正=更省。粗体 = CI 不含 0。)

### 结论

1. **成功率与组织方式无关。** H0–H6 的 39 次成功/失败集合完全一致
   (H5u 例外,见下)。模型做不做得成一条任务由任务本身决定
   (`mock-chat-two-lines` 的自绘编辑器没有可写接口,属设计内不可做;
   `mock-tree-deep-find` 是逐层展开超出 12 步上限),不由工具怎么分组决定。
   **"折叠工具让模型更容易做对"这个假设在第 1 批上不成立。**
2. **折叠反而多花回合,且两个极端点统计显著。** H3(look/do 二分)多
   21.8% 工具回合、H4(极简二元)多 19.5%,两者 CI 都不含 0。合并工具后
   单次回执的定位信息变弱,模型要用更多回合补——静态省下的 schema token
   被动态回合吃掉。
3. **H6(观察合并+动作合并,保留 help/configure/switch)是唯一方向性
   赢家:** token 中位 12707 vs H0 15264(−16.8%),配对 +9.6% 省,
   CI [−2.6, +20.1] 跨 0,差一点显著;成功率、回合持平。若要确认,跑
   confirm 批(N=60 配对)即可——是第 1 批里唯一值得追加样本的方案。
4. **H5(点号命名)在该端点不可跑**:`tools.0.function.name` 带点号被上游
   拒绝(全部 502,"trae 4027"),标记 `NOT_RUNNABLE(endpoint)`。下划线
   变体 H5u 可跑,成功率 −2.6pp(CI [−7.7, +0.0],边缘不显著,一次
   `form-readonly-note` 失败),回合/token 持平——层级写在名字里既没有
   schema 收益也没有行为收益,**且点号形式有兼容性风险**。

**第 1 批裁决:H0 保留(默认不动),H6 作为唯一候选进入 confirm 观察,
H3/H4 的极简方向被证据否决。**

### confirm 批(H0 vs H6,N=65 配对,2026-11-07)

screening 之后对 H6 追加 5 reps(65 配对,MDE 约减半):

| 方案 | 成功率差(pp) | 回合差(%) | token 差(%) | 判定 |
|---|---|---|---|---|
| H6 | −1.5 [−4.6, +0.0] | **−9.9 [−19.1, −1.4]** | +3.7 [−4.4, +10.7] | PENDING |

**confirm 未通过。** 追加样本后:H6 的 token 优势从 +9.6% 缩水到
+3.7%(CI 跨 0);回合差转为显著变多 +9.9%;成功率 −1.5pp(CI 上界恰为 0,
边缘)。screening 批看到的 −16.8% token 中位差是抽样波动,不是真实效应。

### 第 1 批最终裁决

**H0(平铺九工具)保留,七个层级变体全部未被采纳:**

- 极简方向(H3/H4):回合显著变多(CI 不含 0),证据否决;
- 合并方向(H1/H2/H6):中性偏好(H6 略省 token 但未过 confirm);
- 命名方向(H5u):中性略负,且点号形式(H5)有端点兼容性风险;
- 成功率在所有方案上完全一致——任务的成败由任务决定,不由工具组织决定。

对这个模型(deepseek-v4.1-flash)和这批任务,当前平铺工具面就是本批
测量下的最优组织。层级假设的整体否定并不意外:九个工具的静态成本
(1890 token)相对每任务 ~15k 的动态成本只占 ~12%,省 schema 的空间
本来就小,而折叠损害回执定位信息的代价是实打实的。

## live 层确认(deepseek-v4.1-flash,子会话,2026-11-07)

H0 × 5 任务 × 1 rep,全部在子会话执行,断言由独立会话 full 观察后判定:

| 任务 | 结果 | 工具回合 | token |
|---|---|---|---|
| live-store-find-add | PASS | 4 | 15013+227 |
| live-form-submit | PASS | 6 | 23320+913 |
| live-menu-new | PASS | 6 | 21098+454 |
| live-dialog-delete | PASS | 6 | 20557+452 |
| live-launch-form | PASS | 3 | 11112+433 |

**5/5 PASS**——包括 `launch` 在子桌面启动第二个场景再确认窗口出现。
两处 live 专属的装置修正记录在案:① live 行名带价格后缀,断言用子串匹配
(plan-v3 §4.2 同款问题);② 真实 form 应用的校验文本节点不暴露
automation_id(fixture 有、真机没有),该断言只保留在 mock 层。

### 排障记录(端点)

- 模型列表 id `deepseek-v4.1flash` 不可用,实际 id `deepseek-v4.1-flash`;
- 端点上游拒绝带点号的工具名(`tools.0.function.name`,`trae 4027`)→
  H5 整批 502,标记 `NOT_RUNNABLE(endpoint)`,以 H5u(下划线)替代测量;
- 网关偶发 502/503:`LlmClient.chat` 已加三次退避重试。

## 行为等价性验证(已测)

`tests/bench/test_exposure.py` 在任何评分开跑前保证:

1. 每个折叠调用与其平铺调用在 MockProvider 上产出**相同**回执;
2. 七个面的 surface_hash 互异且装配确定;
3. H0 dispatch 与 `call_tool` 逐工具等价,未知工具错误逐字相同;
4. 全部七个面都能把同一条 store 任务驱动到**相同的终态**;
5. 假模型端到端(`test_smoke.py`)在 H0 与 H4 上闭环通过。

live 层(子会话)用假模型冒烟通过:H0 与 H4 都能在真实 ScenarioSuite
窗口上完成 observe→aim→find→add,独立断言会话读到购物车终态。

## 评分结果（已填）

真模型评分已完成，结果就地填在上面的 screening / confirm / live 表中，
不再保留“待跑”占位。mock 层为 7 方案 × 13 任务 × 3 reps，confirm 为
N=65 配对，live 层在子会话内 5/5 PASS。

## 批次索引

- 第 1 批（层级）：见上，最终裁决 H0 保留；
- 第 2 批（命名）：见下节，最终裁决保留 `activate` 别名；
- 第 3 批（暴露时机）：见文末，采纳 T1 原则，落实为默认 `core` 档。

## 第 2 批:命名(层级固定 H0,只改动词名,2026-11-07)

四个单变量命名假设,每个对应一个真实疑问:

| 方案 | 改动 | 假设 |
|---|---|---|
| **N1** | **能力名不做别名**:`activate`→`invoke` | 控件原生暴露 Invoke 模式的能力直接叫 `invoke`,不再别名成意图动词;工具面与层级不动(`somnus/compiler/caps.py` 加 `NATIVE_CAP_NAMES` 开关,默认关) |
| N2 | 工具 `call`→`act` | 名字说意图,不暴露机制 |
| N3 | 工具 `run`→`script` | `run` 与 `launch` 语义相撞,改名消歧 |
| N4 | `observe/find/inspect`→`look/search/peek` | 观察族整体口语化(prompt 正文同步替换) |

### screening(× 13 任务 × 3 reps,基线 H0 复用第 1 批)

| 方案 | 成功率差(pp) | 回合差(%) | token 差(%) | 判定 |
|---|---|---|---|---|
| **N1 不做别名** | +0.0 [+0.0, +0.0] | +10.3 [+0.7, +19.6] | **+10.4 [+1.4, +18.9]** | ADOPT_CANDIDATE |
| N2 act | −2.6 [−7.7, +0.0] | −7.1 [−23.2, +6.6] | −2.2 | PENDING |
| N3 script | −2.6 [−7.7, +0.0] | +4.2 | +5.2 | PENDING |
| N4 look/search/peek | +0.0 [+0.0, +0.0] | +3.4 | +5.5 | PENDING |

(正 = 相对 H0 更省/更好;粗体 = CI 不含 0。)

**N1 是两批实验里第一个真正赢的方案**,而且机制可解释:

- **inspect 调用每任务 1.31 → 0.72(−45%)**。别名 `activate` 让模型不放心
  ——观察里看到 `+activate` 之后还要 inspect 验证一次"这到底是什么";
  原生名 `invoke` 自解释(它就说的是 UIA Invoke 接口),验证回合直接消失。
- 无效调用率同步下降(1.4% → 0.5%)。
- 回合 +10.3% 显著更省(CI 下界 +0.7);token 中位 11836 vs 15264(−22%)。

N2(act)整体偏负:意图动词没有带来路由收益,反而成功率边缘下滑。
N3/N4 中性:改名消歧和口语化既不赚也不亏。

confirm 批(H0 vs N1 × 5 reps)结果见下。

### confirm 批(H0 vs N1,N=65 配对,2026-11-07)

| 方案 | 成功率差(pp) | 回合差(%) | token 差(%) | 判定 |
|---|---|---|---|---|
| N1 不做别名 | +0.0 [+0.0, +0.0] | −1.3 [−9.3, +6.3] | −1.6 [−8.5, +5.1] | PENDING |

(130 次运行,0 次基础设施错误。)

**confirm 未通过。** screening 看到的 token −10.4% / inspect −45% 在 N=65
上没有复现:inspect 每任务 1.03(H0)vs 0.92(N1),差距缩到噪声量级;
回合、token、成功率全部持平。screening 的 ADOPT_CANDIDATE 是 39 次样本上
的抽样波动——confirm 步骤存在价值在两批实验里各被验证了一次(第 1 批的
H6、第 2 批的 N1 都是 screening 赢、confirm 输)。

### 第 2 批最终裁决

**H0 连同 `activate` 别名一起保留。** 四个命名假设全部未被采纳:

- 能力名不做别名(invoke):screening 显著、confirm 归零——别名不携带
  定位信息损失,模型的验证行为没有因命名而改变;
- 工具改名(act/script/look-search-peek):全程中性。

两批合并后的总图景进一步收敛:**对这个模型,名字和信息组织怎么改都不
移动成功率;唯一的显著性来自"折叠伤害回执定位"那一侧(负向)。**
`NATIVE_CAP_NAMES` 开关保留在 `somnus/compiler/caps.py`(默认关),作为
已测中性、可按用户偏好取舍的选项,不作为默认变更的依据。

## 第 3 批:暴露时机(层级命名固定 H0,只动"何时可见什么",2026-11-07)

吸取前两批教训(screening 假阳性 ×2),本批**直接跑 confirm 级样本**,
跳过 screening:每方案 13 任务 × 5 reps = 65 配对,基线为第 2 批 confirm
批的 H0(同 reps 同任务,130 次运行,0 基础设施错误)。

| 方案 | 改动 | 假设 |
|---|---|---|
| **T1** | 只暴露 core 六工具(去掉 switch/launch/configure) | 任务用不到的工具不该出现在面里——等价于线上 MCP 的 core 档 |
| **T2** | 最小起步 observe/call/help,调一次 help 展开全量 | 按需暴露:不用的工具连 schema 都不注入 |

### 结果(N=65 配对,confirm 级)

| 方案 | 成功率差(pp) | 回合差(%) | token 差(%) | 判定 |
|---|---|---|---|---|
| **T1 core-only** | +0.0 [+0.0, +0.0] | −4.1 [−12.5, +3.6] | **+16.1 [+10.2, +21.6]** | **ADOPT_CANDIDATE** |
| T2 help 展开 | +0.0 [+0.0, +0.0] | **−17.9 [−31.7, −5.8]** | +10.8 [+0.1, +20.7] | 条件性(见下) |

(正 = 相对 H0 更省/更好;粗体 = CI 不含 0。token 中位:H0 14615 →
T1 11770(−19.5%)→ T2 12983(−11.2%)。)

### 机制与裁决

**T1 的收益主要是算术,而算术是确定的。** 工具 schema 在**每一次**请求里
都随 prompt 计费:9 工具 → 6 工具,每步省约 300 token,一个 5 回合任务
就是 ~1500;加上 3 个不可用项不再出现在面里,无效调用率 0.9% → 0.3%。
行为侧(成功率、回合)完全不动——它没有让模型变聪明,只是不再为用不到
的入口付费。**这是三批实验里唯一同时满足"confirm 级显著 + 收益机制是
结构性确定"的方案。**

**T2 的净账不划算**:每任务多 1 个 help 展开回合(回合差 −17.9%,CI
不含 0),换来的 token 节省大部分和 T1 同源(schema 只在展开前少注入)。
按需暴露的展开税是实打实的。

### 第 3 批裁决

**采纳 T1 的原则,不是原样照搬**:这批任务不需要 switch/launch/configure,
所以砍掉它们纯赚;真实使用里这三个工具是需要的,正确形态就是**现有
`SOMNUS_CAPABILITIES` 分档的默认收紧**——默认 core 档(六工具),
desktop 档(switch/launch/configure)仅在会话声明需要子桌面/进程启动时
开启。该机制线上早已存在,本批数据第一次给了它量化依据。

### 三批总表

| 批 | 方案数 | 采纳 | 被证据否决 | 中性保留 |
|---|---|---|---|---|
| 1 层级 | 7(+confirm) | 无 | H3/H4(回合显著变多) | H0 |
| 2 命名 | 4(+confirm) | 无 | — | H0 + activate 别名 |
| 3 时机 | 2(confirm 级) | **T1 原则(默认收紧到 core 档)** | T2(展开税) | — |

**总结论:九工具平铺的 H0 在层级与命名上已是最优;唯一有量化依据的改进
是"默认少暴露用不到的工具"(T1/SOMNUS_CAPABILITIES core 档),省 ~19%
token 且零行为代价。**
